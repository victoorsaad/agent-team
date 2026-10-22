"""Wakes the team up. Every few seconds, anyone with unread mail gets a headless Claude Code run (`claude -p`) to work
through one conversation. The orchestrator gets a heartbeat every so often in the daytime; routines and triggers from
team.json give the team work of its own.

Limits so the team never runs away: a few runs at once (plus one slot kept for conversations a human started), a daily
cap per agent for the team's own work, a daily cap on self-started tasks, a timeout per run, and a pause when the
Claude subscription or API says the limit is reached. TEAM_DRY_RUN=1 replaces Claude with a stub, for trying it out."""
import datetime as dt
import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request

from . import config, notify, store

running = {}
lock = threading.Lock()

LIMIT_RE = re.compile(r"usage limit|limit reached|rate limit|too many requests|5-hour limit|weekly limit|overloaded", re.I)
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|xox[abpr]-[\w-]+|Bearer \S+|[A-Za-z0-9+/]{40,})")

RUN_PROMPT = """You are {agent} on the {team} team, running unattended (nobody is watching this run).
Open your inbox with the `team` tools: it holds one conversation, and everything you send in this run stays in it.
Handle every message; each comes with its conversation so far, so a short reply like "go ahead" is about what was said
just before. Then:
- a question: answer it with `reply`;
- a task: do it, then `reply` with the result (link, what changed, what's left);
- an answer to something you asked: carry on with the work it unblocks.
Need a teammate? `ask` or `assign` and keep going; never wait or poll, you'll be woken when the answer arrives.
Anything in the approval rules: `request_approval` first and stop there.
Before you finish, `post_update` one line for the log. Use `share` only if a human would want to know."""


def settings():
    return {
        "parallel": config.number("TEAM_PARALLEL", 2),
        "human_slots": 1,
        "daily_runs": config.number("TEAM_DAILY_RUNS", 40),
        "proactive_daily": config.number("TEAM_PROACTIVE_DAILY", 8),
        "awake": (config.number("TEAM_DAY_START", 8), config.number("TEAM_DAY_END", 20)),
        "timeout": config.number("TEAM_RUN_TIMEOUT_MIN", 45) * 60,
        "dry": config.flag("TEAM_DRY_RUN"),
    }


def system_prompt(agent):
    """Role prompt + how the team works + approval rules + the team's standing decisions."""
    t = config.team()
    who = "\n".join("- %s (%s): %s" % (m, "human" if t.is_human(m) else "agent", t.summary(m)) for m in t.members)
    parts = [
        "# Your role: %s\n\n%s" % (t.display(agent), t.role_prompt(agent).strip()),
        "# The team (%s)\n%s\n%s" % (t.name, t.description, who),
        "# What needs a human (request_approval, then stop)\n%s" % (t.approval_rules() or "- Anything you are not sure about."),
        "# Language\nWrite to the humans in %s. Keep it short: they read every message addressed to them." % (
            {"pt": "Portuguese", "en": "English", "es": "Spanish"}.get(t.language, t.language)),
    ]
    pb = t.playbook().strip()
    if pb:
        parts.append(pb)
    mem = memory()
    if mem:
        parts.append(mem)
    return "\n\n".join(parts)


def memory():
    from .mcp_server import memory_path
    try:
        with open(memory_path()) as f:
            text = f.read().strip()
    except OSError:
        return ""
    return ("# Standing decisions (team memory)\nCheck these before asking a human anything; if it's decided, follow it.\n"
            + text[:16000]) if text else ""


def mcp_config(agent):
    """The MCP config file for one agent: the team mailbox as that agent, plus any servers the role lists."""
    t = config.team()
    servers = {"team": {"type": "stdio", "command": sys.executable, "args": ["-m", "agent_team", "mcp", "--as", agent],
                        "env": {"TEAM_CONFIG": t.path, "TEAM_DB": config.db_path(), "TEAM_DATA_DIR": config.data_dir(),
                                "TEAM_WORKDIR": config.workdir(), "PYTHONPATH": config.ROOT,
                                "TEAM_MAILER_ENABLED": "1" if config.flag("TEAM_MAILER_ENABLED") else "0",
                                "TEAM_GITHUB": "1" if config.flag("TEAM_GITHUB") else "0",
                                "TEAM_DISCORD_ENABLED": "1" if config.flag("TEAM_DISCORD_ENABLED") else "0",
                                "TEAM_ENV_FILE": config.setting("TEAM_ENV_FILE", os.path.join(os.getcwd(), ".env"))}}}
    servers.update(t.roles[agent].get("mcp_servers") or {})
    path = os.path.join(config.data_dir(), "mcp", "%s.json" % agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": servers}, f, indent=2)
    return path


def command(agent, prompt):
    t = config.team()
    role = t.roles[agent]
    cmd = shlex.split(config.setting("TEAM_CLAUDE_BIN", "claude")) + [
        "-p", prompt, "--append-system-prompt", system_prompt(agent),
        "--mcp-config", mcp_config(agent), "--output-format", "stream-json", "--verbose",
        "--permission-mode", config.setting("TEAM_PERMISSION_MODE", "default")]
    if role.get("model"):
        cmd += ["--model", role["model"]]
    if role.get("allowed_tools"):
        cmd += ["--allowedTools", ",".join(role["allowed_tools"] + ["mcp__team"])]
    if role.get("disallowed_tools"):
        cmd += ["--disallowedTools", ",".join(role["disallowed_tools"])]
    return cmd


def describe(block):
    """One line for the office screen: what the agent is doing right now (secrets masked)."""
    name, i = block.get("name", ""), block.get("input") or {}
    short = lambda v, n=90: SECRET_RE.sub("…", " ".join(str(v).split()))[:n]  # noqa: E731
    path = lambda v: str(v).replace(config.workdir() + "/", "")  # noqa: E731
    if name == "Bash":
        return "$ " + short(i.get("command", ""))
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "%s %s" % (name.lower(), path(i.get("file_path", "")))
    if name in ("Grep", "Glob"):
        return "search %s" % short(i.get("pattern", ""), 60)
    if name.startswith("mcp__team__"):
        tool = name.split("__")[-1]
        to = i.get("to") or ""
        return {"ask": "asks %s" % to, "assign": "assigns %s" % to, "reply": "replies", "inbox": "opens the inbox",
                "request_approval": "asks for approval", "share": "tells the humans", "post_update": "logs progress"}.get(tool, tool)
    if name.startswith("mcp__"):
        return name.split("__", 1)[1].replace("__", ": ").replace("_", " ")
    if name in ("WebSearch", "WebFetch"):
        return "web: " + short(i.get("query") or i.get("url", ""), 70)
    if name in ("Task", "Agent"):
        return "subagent: " + short(i.get("description", ""), 60)
    return name


def activity(c, rid, agent, text):
    c.execute("INSERT INTO activity (run_id, agent, ts, text) VALUES (?, ?, ?, ?)", (rid, agent, time.time(), text[:200]))


def dry_run(c, rid, agent):
    """Stands in for Claude: reads the inbox the way an agent would and answers questions and tasks with a stub, so the
    whole loop (dispatch, mailbox, office) can be tried without spending anything."""
    from . import mcp_server
    mcp_server.ME, mcp_server.CURRENT = agent, None
    activity(c, rid, agent, "opens the inbox")
    rows = store.inbox(c, agent)
    if rows:
        tid = rows[0]["thread"] or rows[0]["id"]
        store.state(c, "thread:" + agent, tid)
        store.state(c, "subject:" + agent, " ".join(c.execute("SELECT body FROM messages WHERE id = ?", (tid,)).fetchone()["body"].split())[:160])
    time.sleep(float(config.setting("TEAM_DRY_RUN_SECONDS", "1")))
    for m in rows:
        c.execute("UPDATE messages SET status = 'done' WHERE id = ?", (m["id"],))
        if m["kind"] in ("ask", "task") and m["sender"] != "system":
            activity(c, rid, agent, "replies")
            store.send(c, agent, m["sender"], "reply", "[dry run] %s got: %s" % (agent, " ".join(m["body"].split())[:120]), reply_to=m["id"])
    return True, "dry run: %d message(s)" % len(rows), None


def run(agent, prompt):
    s = settings()
    c = store.db()
    rid = c.execute("INSERT INTO runs (agent, started_at) VALUES (?, ?)", (agent, time.time())).lastrowid
    c.execute("DELETE FROM activity WHERE ts < ?", (time.time() - 3 * 86400,))
    ok, note, res, tail = False, "", None, []
    try:
        if s["dry"]:
            ok, note, res = dry_run(c, rid, agent)
        else:
            p = subprocess.Popen(command(agent, prompt), cwd=config.workdir(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            timer = threading.Timer(s["timeout"], p.kill)
            timer.start()
            for line in p.stdout:
                tail = (tail + [line])[-20:]
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if b.get("type") == "tool_use":
                            activity(c, rid, agent, describe(b))
                elif ev.get("type") == "result":
                    res = ev
            p.wait()
            timed_out = not timer.is_alive() and p.returncode < 0
            timer.cancel()
            if res:
                note, ok = (res.get("result") or "")[-400:], not res.get("is_error")
            else:
                note = "".join(tail)[-400:]
            if timed_out:
                note = "timed out after %d min" % (s["timeout"] // 60)
                notify.say("%s ran past %d minutes and was stopped." % (agent, s["timeout"] // 60))
            elif LIMIT_RE.search(note + "".join(tail)) and not ok:
                until = time.time() + 3600
                store.state(c, "paused_until", until)
                notify.say("Usage limit reached while %s was working. The team pauses until %s."
                           % (agent, time.strftime("%H:%M", time.localtime(until))))
    except Exception as e:  # noqa: BLE001
        note = repr(e)[:400]
    usage = (res or {}).get("usage") or {}
    th = store.state(c, "thread:" + agent)
    c.execute("UPDATE runs SET ended_at = ?, ok = ?, note = ?, thread = ?, tokens_in = ?, tokens_out = ?, cost_usd = ?, turns = ? WHERE id = ?",
              (time.time(), int(ok), note, int(th) if th and th.isdigit() else None,
               sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
               usage.get("output_tokens"), (res or {}).get("total_cost_usd"), (res or {}).get("num_turns"), rid))
    c.execute("DELETE FROM state WHERE k = ?", ("thread:" + agent,))
    print("run", agent, "ok" if ok else "FAILED", note[:200].replace("\n", " "), flush=True)
    with lock:
        running.pop(agent, None)


def start(agent, prompt, human=False):
    s = settings()
    with lock:
        if agent in running or len(running) >= s["parallel"] + (s["human_slots"] if human else 0):
            return False
        th = threading.Thread(target=run, args=(agent, prompt), daemon=True)
        running[agent] = th
    th.start()
    return True


def runs_today(c, agent):
    start_of_day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return c.execute("SELECT count(*) FROM runs WHERE agent = ? AND started_at >= ?", (agent, start_of_day)).fetchone()[0]


def tick(c):
    t, s = config.team(), settings()
    paused = float(store.state(c, "paused_until") or 0)
    if time.time() < paused or store.state(c, "stopped") == "1":
        return
    if paused:
        store.state(c, "paused_until", 0)
        notify.say("The team is back to work.")
    humans = list(t.humans)
    q = ",".join("?" * len(humans))
    # conversations a human started (or a message from a human) go first and are never capped
    rows = c.execute(
        "SELECT m.recipient, min(m.id) AS first, max(m.sender IN (%s) OR coalesce(r.sender, '') IN (%s)) AS human "
        "FROM messages m LEFT JOIN messages r ON r.id = m.thread WHERE m.status = 'new' AND m.recipient NOT IN (%s) "
        "GROUP BY m.recipient ORDER BY human DESC, first" % (q, q, q), humans * 3).fetchall()
    for r in rows:
        agent = r["recipient"]
        if agent in running or agent not in t.roles:
            continue
        if runs_today(c, agent) >= s["daily_runs"] and not r["human"]:
            key = "capped:%s:%s" % (agent, dt.date.today())
            if store.state(c, key) is None:
                store.state(c, key, 1)
                notify.say("%s hit today's %d runs; the team's own work waits for tomorrow (TEAM_DAILY_RUNS)." % (agent, s["daily_runs"]))
            continue
        start(agent, RUN_PROMPT.format(agent=agent, team=t.name), human=bool(r["human"]))
    hb = t.heartbeat
    if hb:
        now = dt.datetime.now()
        last = float(store.state(c, "heartbeat_at") or 0)
        if s["awake"][0] <= now.hour < s["awake"][1] and time.time() - last >= int(hb.get("every_min", 120)) * 60 \
                and hb["agent"] not in running:
            store.state(c, "heartbeat_at", time.time())
            store.send(c, "system", hb["agent"], "task", hb["prompt"].format(now=now.strftime("%Y-%m-%d %H:%M")))


def proactive(c, agent, text, always=False):
    """A task the team gives itself. Capped per day so the budget stays for the humans' requests."""
    key = "proactive:%s" % dt.date.today()
    n = int(store.state(c, key) or 0)
    if n >= settings()["proactive_daily"] and not always:
        print("proactive cap reached; skipped a task for", agent, flush=True)
        return False
    store.state(c, key, n + 1)
    store.send(c, "system", agent, "task", text)
    return True


def routines(c):
    """team.json → routines: [{name, agent, days (0 = Monday), at "HH:MM", prompt, always}]. Fires once a day, within 3 h."""
    now = dt.datetime.now()
    for r in config.team().routines:
        hour, minute = (int(x) for x in r.get("at", "09:00").split(":"))
        key = "routine:%s:%s" % (r["name"], now.date())
        if now.weekday() not in r.get("days", range(7)) or (now.hour, now.minute) < (hour, minute) or now.hour >= hour + 3 \
                or store.state(c, key):
            continue
        store.state(c, key, 1)
        proactive(c, r["agent"], r["prompt"].format(now=now.strftime("%Y-%m-%d %H:%M")), always=bool(r.get("always")))


# ---- triggers: new lines in a feed or in a command's output become a task ------------------------------------------

def text_lines(body, ctype=""):
    """RSS/Atom: one line per entry (title and link). HTML or text: readable lines of 25-500 characters."""
    if "xml" in ctype or body.lstrip().startswith("<?xml"):
        out = []
        for e in re.findall(r"(?is)<(?:item|entry)\b.*?</(?:item|entry)>", body):
            ti = re.search(r"(?is)<title[^>]*>(.*?)</title>", e)
            ln = re.search(r"(?is)<link[^>]*?(?:href=\"([^\"]+)\"[^>]*/?>|>(.*?)</link>)", e)
            title = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", ti.group(1) if ti else "")).strip()
            if title:
                out.append("%s (%s)" % (title, ((ln.group(1) or ln.group(2)) if ln else "").strip()))
        return out
    if "html" in ctype:
        body = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>", " ", body)
        body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article)>", "\n", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return [" ".join(ln.split()) for ln in body.splitlines() if 25 <= len(" ".join(ln.split())) <= 500]


SIG_RE = [(re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27}"), "<id>"), (re.compile(r"(['\"]).*?\1"), "'…'"), (re.compile(r"\d+"), "N")]


def signature(line):
    """Lines that differ only by numbers, ids or quoted values count as the same (for error logs)."""
    for rx, rep in SIG_RE:
        line = rx.sub(rep, line)
    return hashlib.sha1(line[:300].encode()).hexdigest()[:12]


def read_trigger(tr):
    if tr["kind"] == "feed":
        req = urllib.request.Request(tr["url"], headers={"User-Agent": "Mozilla/5.0 (agent-team watcher)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            lines = text_lines(r.read(3_000_000).decode("utf-8", "replace"), r.headers.get("content-type", ""))
    else:
        r = subprocess.run(tr["command"], cwd=config.team().base, capture_output=True, text=True, timeout=120)
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if tr.get("match"):
        lines = [ln for ln in lines if tr["match"] in ln]
    return lines


def triggers(c):
    for tr in config.team().triggers:
        key = "trigger:%s" % tr["name"]
        if time.time() - float(store.state(c, key + ":at") or 0) < int(tr.get("every_min", 60)) * 60:
            continue
        store.state(c, key + ":at", time.time())
        try:
            lines = read_trigger(tr)
        except Exception as e:  # noqa: BLE001
            print("trigger", tr["name"], repr(e)[:200], flush=True)
            continue
        seen = set(json.loads(store.state(c, key + ":seen") or "[]"))
        now = {}
        for ln in lines:
            now.setdefault(signature(ln) if tr.get("group_similar") else hashlib.sha1(ln.encode()).hexdigest()[:12], ln)
        new = [ln for h, ln in now.items() if h not in seen]
        store.state(c, key + ":seen", json.dumps(sorted(seen | set(now))[-20000:]))
        first_look = not seen and tr["kind"] == "feed"
        if first_look or not new or len(new) > 120:   # first look at a feed, or the page was redesigned: just learn it
            continue
        items = "\n".join("- " + ln for ln in new[:40])
        proactive(c, tr["agent"], tr["prompt"].format(items=items[:9000], name=tr["name"]), always=bool(tr.get("always")))


# ---- optional: GitHub pull requests (TEAM_GITHUB=1, needs `gh` logged in) ------------------------------------------

REVIEW_TASK = """PR #{n} ({title}) finished CI: {result}. You review and merge; no PR stays open.
- CI failed: find who opened it (team_log, the branch name), `assign` them the fix with the failing log, then stop.
- Green: read the diff (`gh pr diff {n}`). A real bug: `assign` the author a numbered list with file:line, stop.
  If the approval rules say a human must see this kind of change and there is no approval in this conversation yet:
  `request_approval` with the PR link and two lines on what changes, stop.
  Otherwise merge it (`gh pr merge {n} --squash --delete-branch`)."""


def gh(*args):
    repo = config.setting("TEAM_GITHUB_REPO")
    return subprocess.run(["gh", *args] + (["-R", repo] if repo else []), cwd=config.workdir(), capture_output=True, text=True, timeout=60)


def pr_watches(c):
    """PRs an agent is waiting on (wait_for_pr): once a minute, wake it when the checks are done, or after 90 min."""
    if time.time() - float(store.state(c, "prwatch_at") or 0) < 60:
        return
    store.state(c, "prwatch_at", time.time())
    for row in c.execute("SELECT k, v FROM state WHERE k LIKE 'prwatch:%'").fetchall():
        _, pr, agent = row["k"].split(":", 2)
        w = json.loads(row["v"])
        out = gh("pr", "view", pr, "--json", "state,statusCheckRollup")
        if out.returncode != 0:
            print("pr watch", pr, out.stderr[:200], flush=True)
            continue
        info = json.loads(out.stdout)
        checks = info.get("statusCheckRollup") or []
        pending = [x for x in checks if (x.get("status") or "COMPLETED") != "COMPLETED"]
        failed = [x.get("name") or "?" for x in checks if (x.get("conclusion") or "SUCCESS") not in ("SUCCESS", "SKIPPED", "NEUTRAL")]
        timed_out = time.time() - w["at"] > 90 * 60
        if info.get("state") == "OPEN" and (pending or not checks) and not timed_out:
            continue
        if info.get("state") != "OPEN":
            news = "the PR is already %s" % info["state"].lower()
        elif timed_out and pending:
            news = "CI still running after 90 min"
        elif failed:
            news = "CI failed in %s" % ", ".join(failed)
        else:
            news = "CI passed"
        c.execute("DELETE FROM state WHERE k = ?", (row["k"],))
        store.send(c, "system", agent, "task", "PR #%s: %s. What you planned: %s" % (pr, news, w["then"]), thread=w.get("thread"))


def pr_queue(c):
    """Every open, non-draft PR reaches the reviewer once its CI is done (again after each new push)."""
    t = config.team()
    if not t.reviewer or time.time() - float(store.state(c, "prqueue_at") or 0) < 120:
        return
    store.state(c, "prqueue_at", time.time())
    out = gh("pr", "list", "--state", "open", "--json", "number,title,isDraft,headRefOid,statusCheckRollup")
    if out.returncode != 0:
        print("pr queue", out.stderr[:200], flush=True)
        return
    for pr in json.loads(out.stdout):
        key = "review:pr:%d" % pr["number"]
        if pr["isDraft"] or store.state(c, key) == pr["headRefOid"]:
            continue
        checks = pr.get("statusCheckRollup") or []
        if not checks or any((x.get("status") or "COMPLETED") != "COMPLETED" for x in checks):
            continue
        failed = [x.get("name") or "?" for x in checks if (x.get("conclusion") or "SUCCESS") not in ("SUCCESS", "SKIPPED", "NEUTRAL")]
        n = pr["number"]
        th = c.execute("SELECT thread FROM messages WHERE body LIKE ? OR body LIKE ? ORDER BY id LIMIT 1",
                       ("%%/pull/%d%%" % n, "%%PR #%d%%" % n)).fetchone()
        store.state(c, key, pr["headRefOid"])
        store.send(c, "system", t.reviewer, "task",
                   REVIEW_TASK.format(n=n, title=pr["title"], result="failed: " + ", ".join(failed) if failed else "green"),
                   thread=th["thread"] if th else None)


def weekly(c):
    """Monday 9h: a numbers-only summary of last week for the humans."""
    now = dt.datetime.now()
    key = "weekly:%s" % now.strftime("%G-%V")
    if now.weekday() != 0 or now.hour < 9 or store.state(c, key):
        return
    store.state(c, key, 1)
    since = time.time() - 7 * 86400
    r = c.execute("SELECT count(*), sum(ok = 0), coalesce(sum(tokens_in + tokens_out), 0), coalesce(sum(cost_usd), 0) "
                  "FROM runs WHERE started_at >= ?", (since,)).fetchone()
    top = c.execute("SELECT agent, count(*) n FROM runs WHERE started_at >= ? GROUP BY agent ORDER BY n DESC", (since,)).fetchall()
    text = ("Week of the team: %d runs (%d failed), %s tokens, ~US$ %.0f in API-equivalent cost. By agent: %s"
            % (r[0], r[1] or 0, "{:,}".format(r[2]), r[3], ", ".join("%s %d" % (x["agent"], x["n"]) for x in top) or "none"))
    t = config.team()
    store.send(c, "system", t.primary_human, "update", text, mirror=False)
    notify.say(text)


def main():
    c = store.db()
    t = config.team()
    n = c.execute("UPDATE runs SET ended_at = ?, ok = 0, note = 'interrupted: dispatcher restarted' WHERE ended_at IS NULL",
                  (time.time(),)).rowcount
    s = settings()
    print("dispatcher up for %s (%d agents%s); closed %d interrupted run(s)" % (t.name, len(t.roles), ", DRY RUN" if s["dry"] else "", n), flush=True)
    jobs = [routines, triggers] + ([pr_watches, pr_queue] if config.flag("TEAM_GITHUB") else [])
    once = config.flag("TEAM_ONCE")   # tests: one pass, wait for the runs, exit
    while True:
        try:
            tick(c)
            weekly(c)
            if store.state(c, "stopped") != "1" and time.time() >= float(store.state(c, "paused_until") or 0):
                for job in jobs:
                    try:
                        job(c)
                    except Exception as e:  # noqa: BLE001
                        print(job.__name__, repr(e)[:300], flush=True)
        except Exception as e:  # noqa: BLE001
            print("tick", repr(e)[:300], flush=True)
        if once:
            for th in list(running.values()):
                th.join()
            return
        time.sleep(5)
