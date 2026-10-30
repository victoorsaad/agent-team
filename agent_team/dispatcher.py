"""Wakes the team up. Every few seconds, anyone with unread mail gets a headless Claude Code run (`claude -p`) to work
through one conversation. The heartbeat agent gets a heartbeat every so often in the daytime (or, with "when":
"backlog", only when a PR sits open for more than a day or a new issue appears); routines and triggers from team.json
give the team work of its own.

Mechanical work is code here, not an agent run: the merge gate merges what the reviewer passed, promises wake their
agent when their dependency closes or they fall due, the daily digest is assembled from the tables, and the weekly
retro gets its task with the data it should read.

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

from . import config, daily, knowledge, notify, ship, store

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

# Added to RUN_PROMPT by the optional features in team.json.
PROMPT_DOOR = """
The humans talk only to {orchestrator}: answer whoever asked you (usually {orchestrator}), not the humans."""
PROMPT_PROMISES = """
Anything you'll do after this run ends ("after the deploy", "this afternoon", "once the PR is in"): register it with
`commit` (what, due, depends) before you finish; you're woken for it, and you close it with `commit_done` + proof."""
PROMPT_DIGEST = """
`share` lands in the daily digest; `urgent` only for what can't wait."""
PROMPT_NORTH = """

The team's north this week (until {until}, set by {by}):
{goals}
A human's request comes before everything. The rest of your work serves one of these; say which in your post_update."""
NORTH_GATE = """The team's north this week:
{goals}
The team started this task on its own. Before you start, say in one line which goal it serves. If it serves none and
isn't maintenance (an error in production, security, a broken link, a promise): post_update "off north: <task>" and
stop; the retro reads those lines.

"""


def north(c):
    """The week's goals (`set_north`) as numbered lines, and the record; ("", None) when none or expired."""
    n = json.loads(store.state(c, "north") or "null")
    if not n or n["until"] < dt.date.today().isoformat():
        return "", None
    return "\n".join("%d. %s" % (i + 1, g) for i, g in enumerate(n["goals"])), n


def run_prompt(agent, c=None):
    t = config.team()
    extra = (PROMPT_DOOR.format(orchestrator=t.orchestrator) if t.single_door and agent != t.orchestrator else "") \
        + (PROMPT_PROMISES if t.promises.get("require", t.single_door) else "") + (PROMPT_DIGEST if t.digest else "")
    if t.north and c is not None:
        goals, n = north(c)
        if goals:
            extra += PROMPT_NORTH.format(until=n["until"], by=t.display(n["by"]), goals=goals)
    return RUN_PROMPT.format(agent=agent, team=t.name) + extra


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
    """Role prompt + how the team works + approval rules (+ authority levels) + the team's standing decisions."""
    t = config.team()
    who = "\n".join("- %s (%s): %s" % (m, "human" if t.is_human(m) else "agent", t.summary(m)) for m in t.members)
    parts = [
        "# Your role: %s\n\n%s" % (t.display(agent), t.role_prompt(agent).strip()),
        "# The team (%s)\n%s\n%s" % (t.name, t.description, who),
        "# What needs a human (request_approval, then stop)\n%s" % (t.approval_rules() or "- Anything you are not sure about."),
    ]
    if t.authority_table():
        parts.append("# Authority: who decides what\n%s" % t.authority_table())
    parts += [
        "# Language\nWrite to the humans in %s. Keep it short: they read every message addressed to them." % (
            {"pt": "Portuguese", "en": "English", "es": "Spanish"}.get(t.language, t.language)),
    ]
    pb = t.playbook().strip()
    if pb:
        parts.append(pb)
    mem = memory()
    if mem:
        parts.append(mem)
    notes = knowledge.context()
    if notes:
        parts.append(notes)
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
                                "TEAM_KNOWLEDGE_PATH": config.setting("TEAM_KNOWLEDGE_PATH"),
                                "TEAM_EVAL": "1" if config.flag("TEAM_EVAL") else "0",
                                "TEAM_ENV_FILE": config.setting("TEAM_ENV_FILE", os.path.join(os.getcwd(), ".env"))}}}
    servers.update(t.roles[agent].get("mcp_servers") or {})
    path = os.path.join(config.data_dir(), "mcp", "%s.json" % agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": servers}, f, indent=2)
    return path


LIGHT_TAG = "[light review]"   # in a reviewer task: the PR only touches review.light_paths (docs, copy, styles)


def pick_model(c, agent, human):
    """The model for one run: team.json models.human for a conversation a human started (the strong model where it
    matters), models.light_review for a reviewer task that only touches light paths, else the role's own `model`
    (None: Claude Code's default)."""
    t = config.team()
    m, role = t.models, t.roles[agent]
    if human and m.get("human") and agent not in (m.get("human_except") or []):
        return m["human"]
    if agent == t.reviewer and m.get("light_review"):
        rows = c.execute("SELECT body FROM messages WHERE recipient = ? AND status = 'new'", (agent,)).fetchall()
        if rows and all(LIGHT_TAG in (r["body"] or "") for r in rows):
            return m["light_review"]
    return role.get("model")


def command(agent, prompt, model=None):
    t = config.team()
    role = t.roles[agent]
    cmd = shlex.split(config.setting("TEAM_CLAUDE_BIN", "claude")) + [
        "-p", prompt, "--append-system-prompt", system_prompt(agent),
        "--mcp-config", mcp_config(agent), "--output-format", "stream-json", "--verbose",
        "--permission-mode", config.setting("TEAM_PERMISSION_MODE", "default")]
    if model or role.get("model"):
        cmd += ["--model", model or role["model"]]
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
                "request_approval": "asks for approval", "share": "tells the humans", "post_update": "logs progress",
                "commit": "makes a promise", "commit_done": "keeps a promise", "decide": "records a decision",
                "review": "reviews a PR"}.get(tool, tool)
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


def run(agent, prompt, origin=None, model=None):
    s = settings()
    c = store.db()
    rid = c.execute("INSERT INTO runs (agent, started_at, origin, model) VALUES (?, ?, ?, ?)",
                    (agent, time.time(), origin, model or config.team().roles[agent].get("model") or "default")).lastrowid
    c.execute("DELETE FROM activity WHERE ts < ?", (time.time() - 3 * 86400,))
    ok, note, res, tail = False, "", None, []
    try:
        if s["dry"]:
            ok, note, res = dry_run(c, rid, agent)
        else:
            p = subprocess.Popen(command(agent, prompt, model), cwd=config.workdir(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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


def start(agent, prompt, human=False, origin=None, model=None):
    s = settings()
    with lock:
        if agent in running or len(running) >= s["parallel"] + (s["human_slots"] if human else 0):
            return False
        th = threading.Thread(target=run, args=(agent, prompt, origin, model), daemon=True)
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
        first = c.execute("SELECT sender FROM messages WHERE id = ?", (r["first"],)).fetchone()
        origin = "human" if r["human"] else ("routine" if first and first["sender"] == "system" else "team")
        start(agent, run_prompt(agent, c), human=bool(r["human"]), origin=origin, model=pick_model(c, agent, bool(r["human"])))
    hb = t.heartbeat
    if hb:
        now = dt.datetime.now()
        last = float(store.state(c, "heartbeat_at") or 0)
        if s["awake"][0] <= now.hour < s["awake"][1] and time.time() - last >= int(hb.get("every_min", 120)) * 60 \
                and hb["agent"] not in running:
            store.state(c, "heartbeat_at", time.time())
            why = backlog_news(last) if hb.get("when") == "backlog" else ""
            if why is not None:   # "backlog": only when there's something; nothing to move is not worth a run
                store.send(c, "system", hb["agent"], "task", hb["prompt"].format(now=now.strftime("%Y-%m-%d %H:%M"), why=why))


def proactive(c, agent, text, always=False, maintenance=False):
    """A task the team gives itself. Capped per day so the budget stays for the humans' requests. With a north set
    (team.json north), it has to say which goal it serves or stop; maintenance (errors, the retro) always goes."""
    agent = config.team().who(agent)
    key = "proactive:%s" % dt.date.today()
    n = int(store.state(c, key) or 0)
    if n >= settings()["proactive_daily"] and not always:
        print("proactive cap reached; skipped a task for", agent, flush=True)
        return False
    store.state(c, key, n + 1)
    goals = north(c)[0] if config.team().north else ""
    store.send(c, "system", agent, "task", (NORTH_GATE.format(goals=goals) if goals and not maintenance else "") + text)
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
        proactive(c, r["agent"], r["prompt"].format(now=now.strftime("%Y-%m-%d %H:%M")), always=bool(r.get("always")),
                  maintenance=bool(r.get("maintenance")))


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
        proactive(c, tr["agent"], tr["prompt"].format(items=items[:9000], name=tr["name"]), always=bool(tr.get("always")),
                  maintenance=bool(tr.get("maintenance")))


# ---- optional: GitHub pull requests (TEAM_GITHUB=1, needs `gh` logged in) ------------------------------------------

GATE_REVIEW_TASK = """PR #{n} ({title}) finished CI: {result}. You review; the merge gate merges.{overlap}
- CI failed: find who opened it (team_log, the branch name), `assign` them the fix with the failing log, then stop.
- Green: read the diff (`gh pr diff {n}`), at the depth the risk asks, and note what's wrong before you read the PR's
  description; then check the description's claims against the diff.{guide}{rounds} Then one `review`:
  a real bug → `review` block + `assign` the author a numbered list with file:line;
  fine, but a change the authority levels give to a human and none asked for it in this conversation → `review` ok
  with needs_human and two lines in `what`;
  fine otherwise → `review` ok. Don't merge and don't tell the humans: the gate merges and posts the receipt."""

# Rounds: an agent author and an agent reviewer can loop many times on one PR, most flags after the first pass being
# code that was already there. After round 1 the reviewer looks only at what changed; at the cap it stops listing
# lines and sends the author back to the approach.
ROUND_N = """
  Round {r}: you blocked this PR at {prev}. Look only at what changed since ({since}) and at whether your earlier
  findings were fixed. Don't repeat a finding already posted and don't raise new nits: block only for a real bug in
  the new code or an earlier finding left open."""
ROUND_STOP = """
  Round {r}, the cap: if there is still a bug, don't send another list of lines. `review` block, `assign` the author
  to step back and rethink the approach, naming the class of the problem, and tell {orchestrator} the PR hit the cap."""
OVERLAP = """
It touches the same files as {others}. Same subject (a duplicate, or a fix of a PR merged hours ago that should have
been caught): `review` block and `assign` the author to fold it into the open PR, or to say in the PR why the first
one wasn't enough and what test now covers it. Different subject: ignore this."""


def repo_slug():
    slug = config.setting("TEAM_GITHUB_REPO")
    if not slug:
        out = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        slug = out.stdout.strip()
    return slug


def fast_forward(prev, head):
    """head only adds commits on top of prev. After a rebase a compare would show everything the base got since."""
    try:
        return gh_json("api", "repos/%s/compare/%s...%s" % (repo_slug(), prev, head), "--jq", "{status: .status}")["status"] == "ahead"
    except Exception as e:  # noqa: BLE001
        print("compare", repr(e)[:200], flush=True)
        return False


def review_round(c, n, head):
    """Counts a review round of PR n (the first green head, and a green head after a block; a rebase of a PR that
    passed is not a new round) and returns the text for the reviewer's task. review.max_rounds (default 3)."""
    cap = int(config.team().review.get("max_rounds", 3))
    r, prev, last = int(store.state(c, "rounds:%d" % n) or 0), store.state(c, "green:%d" % n), store.state(c, "lastverdict:%d" % n)
    counts = r == 0 or last == "block"
    if counts:
        r += 1
        store.state(c, "rounds:%d" % n, r)
        store.state(c, "lastverdict:%d" % n, "pending")
    store.state(c, "green:%d" % n, head)
    if not cap or not counts or r < 2 or not prev:
        return ""
    if r >= cap:
        return ROUND_STOP.format(r=r, orchestrator=config.team().orchestrator)
    since = ("`gh api repos/%s/compare/%s...%s`" % (repo_slug(), prev[:12], head[:12]) if fast_forward(prev, head) else
             "the branch was rebased, so read the whole `gh pr diff %d` or `git range-diff %s...%s`" % (n, prev[:12], head[:12]))
    return ROUND_N.format(r=r, prev=prev[:12], since=since)


def overlap(n, files):
    """Open PRs and PRs merged in the last 24 h touching most of the same files as PR n (duplicates, fix-of-a-fix)."""
    ignore = set(config.team().review.get("overlap_ignore") or ["CHANGELOG.md"])
    mine = {f["path"] for f in files} - ignore
    if not mine:
        return ""
    since = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    try:
        others = gh_json("pr", "list", "--state", "open", "--json", "number,title,files") + \
            gh_json("pr", "list", "--state", "merged", "--search", "merged:>=%s" % since, "--json", "number,title,files")
    except Exception as e:  # noqa: BLE001
        print("overlap", repr(e)[:200], flush=True)
        return ""

    def same(o):   # most of this PR's files; a one-file PR only against another small PR
        theirs = {f["path"] for f in o.get("files") or []} - ignore
        shared = len(mine & theirs)
        return o["number"] != n and shared * 2 >= len(mine) and (shared >= 2 or len(theirs) <= 3)
    hits = ["#%d (%s)" % (o["number"], o["title"][:60]) for o in others if same(o)]
    return OVERLAP.format(others=", ".join(hits[:3])) if hits else ""


REVIEW_TASK = """PR #{n} ({title}) finished CI: {result}. You review; the merge gate merges.
- CI failed: find who opened it (team_log, the branch name), `assign` them the fix with the failing log, then stop.
- Green: read the diff (`gh pr diff {n}`), at the depth the risk asks. Then one `review`:
  a real bug → `review` block + `assign` the author a numbered list with file:line;
  fine, but a change the authority levels give to a human and none asked for it in this conversation → `review` ok
  with needs_human and two lines in `what`;
  fine otherwise → `review` ok. Don't merge and don't tell the humans: the gate merges and posts the receipt."""

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
    """Every open, non-draft PR reaches the reviewer once its CI is done (again after each new push). With the merge
    gate: review rounds (review.max_rounds), overlapping PRs flagged (review.overlap), a light-paths tag that lets
    models.light_review pick a cheaper model, and review.guide (a REVIEW.md) named in the task."""
    t = config.team()
    if not t.reviewer or time.time() - float(store.state(c, "prqueue_at") or 0) < 120:
        return
    store.state(c, "prqueue_at", time.time())
    out = gh("pr", "list", "--state", "open", "--json", "number,title,isDraft,headRefOid,headRefName,statusCheckRollup,files")
    if out.returncode != 0:
        print("pr queue", out.stderr[:200], flush=True)
        return
    rv = t.review
    light_rx = re.compile(rv["light_paths"]) if rv.get("light_paths") else None
    skip = tuple(rv.get("skip_branches") or ())
    for pr in json.loads(out.stdout):
        key = "review:pr:%d" % pr["number"]
        if pr["isDraft"] or store.state(c, key) == pr["headRefOid"] or (skip and pr.get("headRefName", "").startswith(skip)):
            continue
        checks = pr.get("statusCheckRollup") or []
        if not checks or any((x.get("status") or "COMPLETED") != "COMPLETED" for x in checks):
            continue
        failed = [x.get("name") or "?" for x in checks if (x.get("conclusion") or "SUCCESS") not in ("SUCCESS", "SKIPPED", "NEUTRAL")]
        n = pr["number"]
        th = c.execute("SELECT thread FROM messages WHERE body LIKE ? OR body LIKE ? ORDER BY id LIMIT 1",
                       ("%%/pull/%d%%" % n, "%%PR #%d%%" % n)).fetchone()
        store.state(c, key, pr["headRefOid"])
        result = "failed: " + ", ".join(failed) if failed else "green"
        if t.merge_gate is not None:
            files = pr.get("files") or []
            light = not failed and light_rx and files and all(light_rx.search(f["path"]) for f in files)
            text = (LIGHT_TAG + " " if light else "") + GATE_REVIEW_TASK.format(
                n=n, title=pr["title"], result=result,
                overlap="" if failed or not rv.get("overlap", True) else overlap(n, files),
                guide=(" %s says what failure looks like here." % rv["guide"]) if rv.get("guide") else "",
                rounds="" if failed else review_round(c, n, pr["headRefOid"]))
        else:
            text = REVIEW_TASK.format(n=n, title=pr["title"], result=result)
        store.send(c, "system", t.reviewer, "task", text, thread=th["thread"] if th else None)


def gh_json(*args):
    out = gh(*args)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:200])
    return json.loads(out.stdout or "null")


def iso_ts(v):
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def backlog_news(since):
    """Why the heartbeat agent should look at the backlog now (heartbeat "when": "backlog"), or None: a ready PR open
    for more than a day, or an issue opened since the last look. Needs TEAM_GITHUB; without it there's never news."""
    if not config.flag("TEAM_GITHUB"):
        return None
    try:
        prs = gh_json("pr", "list", "--state", "open", "--json", "number,isDraft,createdAt")
        issues = gh_json("issue", "list", "--state", "open", "--json", "number,createdAt")
    except Exception as e:  # noqa: BLE001
        print("backlog", repr(e)[:200], flush=True)
        return None
    stale = [p["number"] for p in prs if not p["isDraft"] and time.time() - iso_ts(p["createdAt"]) > 86400]
    new = [i["number"] for i in issues if iso_ts(i["createdAt"]) > since]
    parts = (["PR open for more than a day: " + ", ".join("#%d" % n for n in stale)] if stale else []) + \
            (["new issue: " + ", ".join("#%d" % n for n in new)] if new else [])
    return "; ".join(parts) or None


# ---- the merge gate (team.json merge_gate, TEAM_GITHUB=1) ----------------------------------------------------------

def gate(pr, verdict, decisions, sensitive=None, require_ci=True):
    """What the merge gate does with one reviewed PR: ("drop" | "wait" | "ask" | "merge", why). Pure, so it's tested.
    pr: `gh pr view --json state,isDraft,headRefOid,files,mergeable,statusCheckRollup`; verdict: the reviewer's
    {"head", "verdict", "at"}; decisions: this PR's rows in `decisions`; sensitive: the regex of paths that need a
    human's yes even when the reviewer didn't ask for one."""
    if pr["state"] != "OPEN" or pr["headRefOid"] != verdict["head"]:
        return "drop", "merged, closed or pushed again since the review (the reviewer gets it again)"
    if verdict["verdict"] != "ok":
        return "wait", "the reviewer blocked it"
    if pr.get("isDraft"):
        return "wait", "draft"
    if pr.get("mergeable") == "CONFLICTING":
        return "wait", "conflicts with the base branch"
    checks = pr.get("statusCheckRollup") or []
    if any((x.get("status") or "COMPLETED") != "COMPLETED" for x in checks) or (require_ci and not checks):
        return "wait", "CI hasn't finished on this commit"
    failed = [x.get("name") or "?" for x in checks if (x.get("conclusion") or "SUCCESS") not in ("SUCCESS", "SKIPPED", "NEUTRAL")]
    if failed:
        return "wait", "CI failed: " + ", ".join(failed)
    for d in decisions:
        if d["status"] == "pending":
            return "wait", "decision %d waits for a human" % d["id"]
        if d["status"] == "rejected" and (d["decided_at"] or 0) > verdict["at"]:
            return "wait", "a human rejected it (decision %d)" % d["id"]
    touchy = [f["path"] for f in pr.get("files") or [] if sensitive and sensitive.search(f["path"])]
    if touchy and not any(d["status"] == "approved" for d in decisions):
        return "ask", ", ".join(touchy[:4])
    return "merge", ""


# ---- prompt evals inside the merge gate (team.json prompt_eval) -------------------------------------------------

def pr_author(c, branch):
    """The agent that created the branch (its `git checkout -b` / `worktree add -b` in the activity log), or None."""
    r = c.execute("SELECT agent FROM activity WHERE text LIKE ? OR text LIKE ? ORDER BY id LIMIT 1",
                  ("%%-b %s%%" % branch, "%%-B %s%%" % branch)).fetchone()
    return r["agent"] if r else None


def fallback_owner():
    t = config.team()
    return t.review.get("fallback_owner") or t.orchestrator


def eval_alive(pid):
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)   # our child: reap it if it finished
        return done == 0
    except ChildProcessError:   # not ours (the dispatcher restarted): is the pid still there?
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def locked_lines(n, spec):
    """Changed lines (added or removed) in prompt files that match prompt_eval.locked: a human's call."""
    if not spec.get("locked"):
        return []
    paths, locked = re.compile(spec.get("paths") or "$^"), re.compile(spec["locked"], re.I)
    diff = gh("pr", "diff", str(n)).stdout or ""
    hits, cur = [], None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif cur and paths.search(cur) and line[:1] in "+-" and not line.startswith(("+++", "---")) and locked.search(line):
            hits.append("%s: %s" % (cur, line[1:].strip()[:120]))
    return hits


def prompt_eval(c, n, head, branch, files, thread):
    """The eval for a PR that changes how agents behave: True passed, False failed, None running or queued (one at a
    time). A new push stops the old commit's run; a run that died without a result is retried twice."""
    t = config.team()
    spec = t.prompt_eval or {}
    key = "eval:%d" % n
    ev = json.loads(store.state(c, key) or "null")
    if ev and ev["head"] != head:
        if ev["status"] == "running" and eval_alive(ev.get("pid")):
            try:
                os.killpg(ev["pid"], 15)
            except OSError:
                pass
        c.execute("DELETE FROM state WHERE k = ?", (key,))
        ev = None
    tries = 0
    if ev and ev["status"] == "running" and not os.path.exists(ev["out"]) and not eval_alive(ev.get("pid")) and ev.get("tries", 0) < 2:
        tries = ev.get("tries", 0) + 1
        c.execute("DELETE FROM state WHERE k = ?", (key,))
        ev = None
    wt = os.path.join(config.data_dir(), "eval")
    if ev:
        if ev["status"] != "running":
            return ev["status"] == "pass"
        if not os.path.exists(ev["out"]):
            if eval_alive(ev.get("pid")) and time.time() - ev["at"] < 3 * 3600:
                return None
            ev.update(status="error")
            store.state(c, key, json.dumps(ev))
            store.send(c, "system", fallback_owner(), "task", "The prompt eval of PR #%d didn't finish (log: %s), so the PR doesn't go "
                       "in. Fix the eval or run it by hand (`agent-team eval --root <pr> --base <base>`), then delete state %s."
                       % (n, ev["out"][:-5] + ".log", key), thread=thread)
            return False
        with open(ev["out"]) as f:
            res = json.load(f)
        bad = [r for r in res if r.get("regression")]
        gate_rows = [r for r in res if "hits" not in r]
        sc = [r for r in res if "hits" in r]
        ev.update(status="fail" if bad else "pass", why="; ".join("%s (%s)" % (r["id"], r["agent"]) for r in bad),
                  score="%d/%d" % (sum(r["ok"] for r in gate_rows), len(gate_rows)))
        who = pr_author(c, branch) or fallback_owner()
        if sc:
            pr_h, base_h, runs = sum(r["hits"] for r in sc), sum(r.get("base_hits", 0) for r in sc), sum(r["runs"] for r in sc)
            ev["incidents"] = "incident cases: PR %d/%d, base %d/%d" % (pr_h, runs, base_h, runs)
            if pr_h < base_h:   # doesn't block (noisy), but the author decides with the number in hand
                store.send(c, "system", who, "task", "PR #%d changes the reviewer and it caught fewer real incidents than the base "
                           "(%s; details in %s). Not blocking: adjust the instruction or say in the PR why it's worth it."
                           % (n, ev["incidents"], ev["out"]), thread=thread)
        store.state(c, key, json.dumps(ev))
        if ev["status"] == "fail":
            store.send(c, "system", who, "task", "The prompt eval failed PR #%d: %s. These situations pass on the base and failed "
                       "with the PR (case, why and check in %s; what the agent did in %s). Adjust the instruction and push; the "
                       "eval runs again on the new commit." % (n, ev["why"], spec.get("cases", "evals/cases.json"), ev["out"]), thread=thread)
        else:
            store.did(c, t.reviewer, "Prompt eval of PR #%d: %s, no regression%s" % (n, ev["score"], "; " + ev["incidents"] if ev.get("incidents") else ""),
                      thread=thread)
        return ev["status"] == "pass"
    for row in c.execute("SELECT v FROM state WHERE k LIKE 'eval:%'").fetchall():
        other = json.loads(row["v"])
        if other["status"] == "running" and not os.path.exists(other["out"]):
            return None   # one at a time
    git = lambda *a: subprocess.run(["git", "-C", config.workdir(), *a], capture_output=True, text=True, timeout=300)   # noqa: E731
    base = (t.merge_gate or {}).get("base", "main")
    git("fetch", "-q", "origin", base, "pull/%d/head" % n)
    for w, ref in (("pr", head), ("base", "origin/" + base)):
        git("worktree", "remove", "--force", os.path.join(wt, w))
        if git("worktree", "add", "-q", "--detach", os.path.join(wt, w), ref).returncode != 0:
            return None
    team_dir = os.path.dirname(spec.get("config", "team.json"))
    prompt_files = {}
    for k, r in t.roles.items():
        if r.get("prompt_file"):
            prompt_files[os.path.normpath(os.path.join(team_dir, r["prompt_file"]))] = k
    paths = re.compile(spec.get("paths") or "$^")
    touched = [f["path"] for f in files if paths.search(f["path"])]
    who = {prompt_files[p] for p in touched if p in prompt_files}
    only = bool(who) and all(p in prompt_files for p in touched)
    rv_paths = re.compile(spec["reviewer_paths"]) if spec.get("reviewer_paths") else None
    score = bool(rv_paths and any(rv_paths.search(p) for p in touched))
    if score and t.reviewer:
        who.add(t.reviewer)
    os.makedirs(wt, exist_ok=True)
    out = os.path.join(wt, "pr%d-%s.json" % (n, head[:7]))
    cmd = [sys.executable, "-m", "agent_team", "eval", "--root", os.path.join(wt, "pr"), "--base", os.path.join(wt, "base"), "--json", out]
    if only:
        cmd += ["--agents", ",".join(sorted(who))]
    if score:
        cmd.append("--score")
    with open(out[:-5] + ".log", "w") as log:
        p = subprocess.Popen(cmd, cwd=config.ROOT, start_new_session=True, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    store.state(c, key, json.dumps({"head": head, "status": "running", "out": out, "at": time.time(), "pid": p.pid, "tries": tries}))
    print("eval started for PR", n, flush=True)
    return None


def receipt_from():
    t = config.team()
    return t.orchestrator if t.single_door else t.reviewer


def merge_gate(c):
    """Merges what the reviewer passed (`review` ok), at most one a minute, never around a pending or rejected decision,
    and asks a human first for sensitive paths. A PR from a human's conversation gets a one-line receipt there."""
    t = config.team()
    g = t.merge_gate
    if g is None or not t.reviewer or time.time() - float(store.state(c, "gate_at") or 0) < 60:
        return
    store.state(c, "gate_at", time.time())
    for row in c.execute("SELECT k, v FROM state WHERE k LIKE 'verdict:%'").fetchall():
        n, rv = int(row["k"].split(":")[1]), json.loads(row["v"])
        info = gh_json("pr", "view", str(n), "--json", "state,isDraft,headRefOid,headRefName,title,url,files,mergeable,statusCheckRollup")
        ref = "pr:%d" % n
        decisions = [dict(d) for d in c.execute("SELECT * FROM decisions WHERE ref = ?", (ref,))]
        what, why = gate(info, rv, decisions, t.gate_paths(), g.get("require_ci", True))
        if what == "drop":
            c.execute("DELETE FROM state WHERE k = ?", (row["k"],))
            continue
        if what in ("ask", "merge") and t.prompt_eval is not None:
            spec = t.prompt_eval
            if any(re.search(spec.get("paths") or "$^", f["path"]) for f in info.get("files") or []):
                if not prompt_eval(c, n, rv["head"], info.get("headRefName", ""), info.get("files") or [], rv.get("thread")):
                    continue
                locked = locked_lines(n, spec)
                if locked and not any(d["status"] == "approved" for d in decisions):
                    what, why = "ask", (why + ", " if what == "ask" else "") + "a locked rule (%s)" % "; ".join(locked[:2])
        if what == "ask":
            store.open_decision(c, receipt_from(), "pr", LABELS[lang()]["gate_ask"] % (n, info["title"], why, info.get("url", "")),
                                thread=rv.get("thread"), ref=ref, owner=t.reviewer)
            continue
        if what != "merge":
            continue
        if g.get("deploy_workflow"):
            deploy = gh_json("run", "list", "--workflow", g["deploy_workflow"], "--limit", "1", "--json", "status")
            if deploy and deploy[0]["status"] != "completed":
                return
        out = gh("pr", "merge", str(n), "--%s" % g.get("method", "squash"), "--delete-branch")
        if out.returncode != 0:
            print("gate merge", n, out.stderr[:200], flush=True)
            continue
        c.execute("DELETE FROM state WHERE k = ?", (row["k"],))
        th = rv.get("thread")
        store.did(c, t.reviewer, "PR #%d merged: %s" % (n, info["title"]), proof=info.get("url"), thread=th)
        root = th and c.execute("SELECT sender FROM messages WHERE id = ?", (th,)).fetchone()
        if root and t.is_human(root["sender"]):
            store.state(c, "deploy_now", 1)   # a human is waiting for it: the deploy train doesn't batch it
            store.send(c, receipt_from(), root["sender"], "update", LABELS[lang()]["merged"] % (n, info["title"]), thread=th)
        print("gate merged", n, flush=True)
        return   # one per minute: each merge may deploy


# ---- promises (`commit`) -------------------------------------------------------------------------------------------

PROMISE_TASK = """Promise {id}: {what}
{news} Do it now and close it with `commit_done` (with the proof) or `commit_failed` (why, and what happens instead);
then answer whoever asked, in this conversation."""


def dep_news(c, dep, cache):
    """What happened to a promise's dependency, or None while it's still open."""
    kind, ref = dep.split(":")
    if kind == "decision":
        d = c.execute("SELECT status FROM decisions WHERE id = ?", (int(ref),)).fetchone()
        return "Decision %s was %s." % (ref, d["status"]) if d and d["status"] != "pending" else None
    if not config.flag("TEAM_GITHUB"):
        return None
    if dep not in cache:
        closed, what = ship.deployed(c, ref, cache)   # merged, and deployed when there's a deploy train
        cache[dep] = what if closed else None
    return cache[dep]


def promises(c, now=None):
    """Wake the agent when the dependency closes or at the due time, whichever comes first; an open promise past due by
    promises.chase_after_min (default 120) wakes the orchestrator, once."""
    if now is None:
        if time.time() - float(store.state(c, "promises_at") or 0) < 60:
            return
        store.state(c, "promises_at", time.time())
    t, now, cache = config.team(), now or time.time(), {}
    chase = int(t.promises.get("chase_after_min", 120)) * 60
    for p in c.execute("SELECT * FROM promises WHERE status = 'open' ORDER BY id").fetchall():
        if not p["woke_at"]:
            try:
                news = dep_news(c, p["depends"], cache) if p["depends"] else None
            except Exception as e:  # noqa: BLE001
                print("promise dep", p["id"], repr(e)[:200], flush=True)
                continue
            if news is None and now >= p["due"]:
                news = "It's the time you set%s." % (" (%s still open)" % p["depends"] if p["depends"] else "")
            if news:
                c.execute("UPDATE promises SET woke_at = ? WHERE id = ?", (now, p["id"]))
                store.send(c, "system", p["agent"], "task", PROMISE_TASK.format(id=p["id"], what=p["what"], news=news), thread=p["thread"])
        elif not p["chased_at"] and now >= max(p["due"], p["woke_at"]) + chase and p["agent"] != t.orchestrator:
            c.execute("UPDATE promises SET chased_at = ? WHERE id = ?", (now, p["id"]))
            store.send(c, "system", t.orchestrator, "task", "Promise %d by %s is overdue and still open: %s. Chase the owner once or "
                       "reassign it; if a human asked for it, give them the honest receipt (\"couldn't + why\") in the conversation."
                       % (p["id"], p["agent"], p["what"]), thread=p["thread"])


# ---- the daily digest (team.json digest) ---------------------------------------------------------------------------

LABELS = {
    "en": {"today": "Today · %s", "done": "Done (%d):", "more": " and %d more", "found": "The team found:",
           "decided": "Decided today:", "waiting": "Waiting for you:", "since": "for %s", "mails": "%d email(s) to approve",
           "promises": "Open promises (%d, %d late):", "until": "due %s",
           "footer": "%d requests from you · %d promises kept · %d runs, ~US$ %.2f",
           "merged": "Done: PR #%d merged (%s).", "gate_ask": "PR #%d (%s) touches %s, so it needs a human's yes before it goes in. The reviewer passed it.\n%s"},
    "pt": {"today": "Hoje · %s", "done": "Feito (%d):", "more": " e mais %d", "found": "O time achou:",
           "decided": "Decidido hoje:", "waiting": "Esperando vocês:", "since": "há %s", "mails": "%d e-mail(s) para aprovar",
           "promises": "Promessas abertas (%d, %d atrasadas):", "until": "até %s",
           "footer": "%d pedidos de vocês · %d promessas cumpridas · %d execuções, ~US$ %.2f",
           "merged": "Feito: PR #%d mergeado (%s).", "gate_ask": "O PR #%d (%s) mexe em %s, então precisa do sim de uma pessoa antes de entrar. O revisor aprovou.\n%s"},
}


def lang():
    return config.team().language if config.team().language in LABELS else "en"


def digest_text(c, now=None):
    """The day in one message, assembled from the tables (no agent run): what got done, what the team found (the
    shares that weren't urgent), what was decided, what waits for the humans, open promises, the cost. None when the
    day had nothing at all."""
    t, L = config.team(), LABELS[lang()]
    now = now or time.time()
    day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    clip = lambda x, n=110: (lambda y: y if len(y) <= n else y[:n - 1] + "…")(" ".join((x or "").split()))   # noqa: E731
    age = lambda ts: "%dh" % ((now - ts) // 3600) if now - ts < 2 * 86400 else "%dd" % ((now - ts) // 86400)   # noqa: E731
    humans = list(t.humans)
    q = ",".join("?" * len(humans))
    done = c.execute("SELECT * FROM done WHERE ts >= ? AND agent NOT IN (%s) ORDER BY id" % q, [day] + humans).fetchall()
    shares = c.execute("SELECT sender, body FROM messages WHERE kind = 'share' AND created_at >= ? ORDER BY id", (day,)).fetchall()
    decided = c.execute("SELECT * FROM decisions WHERE decided_at >= ? ORDER BY id", (day,)).fetchall()
    waiting = c.execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY id").fetchall()
    mails = c.execute("SELECT count(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
    open_p = c.execute("SELECT * FROM promises WHERE status = 'open' ORDER BY due").fetchall()
    kept = c.execute("SELECT count(*) FROM promises WHERE status = 'done' AND closed_at >= ?", (day,)).fetchone()[0]
    asked = c.execute("SELECT count(*) FROM messages WHERE sender IN (%s) AND created_at >= ?" % q, humans + [day]).fetchone()[0]
    runs = c.execute("SELECT count(*), coalesce(sum(cost_usd), 0) FROM runs WHERE started_at >= ? AND started_at <= ?", (day, now)).fetchone()
    if not (done or shares or decided or waiting or mails or open_p or asked or runs[0]):
        return None
    lines = ["**%s**" % (L["today"] % time.strftime("%d/%m", time.localtime(now)))]
    if done:
        lines.append("**%s** %s" % (L["done"] % len(done), "; ".join("%s: %s" % (t.display(d["agent"]), clip(d["text"], 70)) for d in done[:6])
                                   + (L["more"] % (len(done) - 6) if len(done) > 6 else "")))
    if shares:
        lines.append("**%s**\n" % L["found"] + "\n".join("· %s: %s" % (t.display(r["sender"]), clip(r["body"])) for r in shares[:8]))
    if decided:
        lines.append("**%s** " % L["decided"] + "; ".join("%s %s (%s)" % ({"approved": "✅", "rejected": "❌"}.get(d["status"], "·"),
                                                                      clip(d["what"], 60), t.display(d["decided_by"] or "")) for d in decided[:6]))
    if waiting or mails:
        lines.append("**%s**\n" % L["waiting"] + "\n".join(["· %s (%s)" % (clip(d["what"], 90), L["since"] % age(d["created_at"])) for d in waiting[:6]]
                                                            + (["· " + L["mails"] % mails] if mails else [])))
    if open_p:
        late = [p for p in open_p if p["due"] < now]
        lines.append("**%s**\n" % (L["promises"] % (len(open_p), len(late))) + "\n".join(
            "· %s%s: %s (%s)" % ("⚠️ " if p["due"] < now else "", t.display(p["agent"]), clip(p["what"], 80),
                                L["until"] % time.strftime("%d/%m %H:%M", time.localtime(p["due"]))) for p in open_p[:6]))
    lines.append(L["footer"] % (asked, kept, runs[0], runs[1]))
    return "\n".join(lines)[:int((t.digest or {}).get("max_chars", 3800))]


def digest(c):
    """team.json digest: {"at": "19:00", "days": [0..6]}. Once a day, within 3 h of `at`, from the orchestrator."""
    t = config.team()
    if not t.digest:
        return
    now = dt.datetime.now()
    hour, minute = t.at(t.digest)
    key = "digest:%s" % now.date()
    if now.weekday() not in t.digest.get("days", range(7)) or (now.hour, now.minute) < (hour, minute) or now.hour >= hour + 3 \
            or store.state(c, key):
        return
    store.state(c, key, 1)
    text = digest_text(c)
    if text:
        store.send(c, t.orchestrator, t.primary_human, "digest", text, mirror=False)
        notify.say(text, who=t.display(t.orchestrator))


# ---- the weekly retro (team.json retro) ----------------------------------------------------------------------------

RETRO_TASK = """Weekly retro ({now}). You audit how the team worked in the last 7 days, {orchestrator} included, and
propose at most {max} changes. Read the playbook first, and last week's retro in {notes}/.
Data, read-only: the team's database (`sqlite3 -readonly {db}`): runs (agent, origin, cost_usd, note), messages
(kind, sender, recipient, thread), decisions, promises, done, outbox{gh}.
Measure, with numbers and two or three examples each (message or run ids): human requests without a receipt;
promises overdue or closed without proof; messages that promise something later without a promise; decisions that
repeat (candidates for a precedent) and categories approved {streak} times in a row without changes (candidates to move
down a level); requests for approval per day; empty or repeated runs; cost per agent and per result; broken rules.
Check whether last week's changes moved the metric they promised; revert by PR what didn't.
Deliver {notes}/YYYY-MM-DD.md (numbers, what got better, what got worse, the changes) and at most {max} PRs, one per
change, in the role prompts or the playbook, each with the metric it should move.
Never change: {locks}. {merge}
End with one `share` of three lines."""
RETRO_HUMAN = "Every PR of yours waits for a human: `request_approval` kind pr with the number and two lines."
RETRO_SELF = ("Your PR merges like any other: the reviewer reviews it and the prompt eval runs before the merge; if it fails "
              "you get the cases and adjust. A changed line touching a locked rule makes the gate ask a human. If the week "
              "showed a real situation the team got wrong and no eval case covers it, add the case in the same PR.")


def retro_task(now):
    t = config.team()
    r = t.retro or {}
    return RETRO_TASK.format(now=now.strftime("%Y-%m-%d %H:%M"), orchestrator=t.orchestrator, max=int(r.get("max_changes", 3)),
                             notes=r.get("notes_dir", "docs/retro"), db=config.db_path(), streak=int(r.get("streak", 10)),
                             gh="; and `gh pr list --state merged`" if config.flag("TEAM_GITHUB") else "",
                             locks=", ".join(t.locks()),
                             merge=RETRO_SELF if r.get("self_merge") and t.prompt_eval is not None else RETRO_HUMAN)


def retro(c):
    """team.json retro: {"agent", "days": [0], "at": "08:30"}. Never skipped for the daily cap (it's weekly)."""
    t = config.team()
    r = t.retro
    if not r:
        return
    now = dt.datetime.now()
    hour, minute = t.at(dict(r, at=r.get("at", "08:30")))
    key = "retro:%s" % now.date()
    if now.weekday() not in r.get("days", [0]) or (now.hour, now.minute) < (hour, minute) or now.hour >= hour + 3 or store.state(c, key):
        return
    store.state(c, key, 1)
    proactive(c, r["agent"], retro_task(now), always=True, maintenance=True)


# ---- the week's north (team.json north) ----------------------------------------------------------------------------

NORTH_TASK = """The team's north for the week ({now}). You set the week's goals for the whole team, so nobody has to ask:
1. Read last week's north (at the end of your instructions, if it's still valid) and measure each goal with real
   numbers: the digest, the retro of this week, the standing decisions, the humans' notes{sources}. Hit, missed or
   halfway, with the number.
2. Pick 1 to 3 goals for this week, each with a number and a date, that move what the humans care about most (the
   playbook and their notes say what that is). Goals of outcome, not activity (not "PRs" or "posts"). A goal nobody
   on the team can work on doesn't go in.
3. `set_north` with the goals and the why (the numbers from step 1).
4. `share` (not urgent) in 3 lines: the goals and last week's result. The humans change the north by answering you;
   nobody approves it first."""


def north_job(c):
    """team.json north: {"agent" (default the orchestrator), "days": [0], "at": "09:15", "sources"}. Weekly."""
    t = config.team()
    n = t.north
    if not n:
        return
    now = dt.datetime.now()
    hour, minute = t.at(dict(n, at=n.get("at", "09:15")))
    key = "north:%s" % now.date()
    if now.weekday() not in n.get("days", [0]) or (now.hour, now.minute) < (hour, minute) or now.hour >= hour + 3 or store.state(c, key):
        return
    store.state(c, key, 1)
    src = n.get("sources")
    proactive(c, n.get("agent") or t.orchestrator, NORTH_TASK.format(now=now.strftime("%Y-%m-%d %H:%M"), sources="; " + src if src else ""),
              always=True, maintenance=True)


# ---- the daily note in the humans' notes (team.json knowledge) -----------------------------------------------------

def daily_note(c, day=None):
    """Once a day at knowledge.daily_at (23:50), the day's note (daily.py) goes to the knowledge provider."""
    t = config.team()
    k = t.knowledge
    if not k.get("provider") or k.get("daily") is False:
        return None
    if day is None:
        now = dt.datetime.now()
        h, m = t.at(dict(k, at=k.get("daily_at", "23:50")))
        key = "daily_note:%s" % now.date()
        if (now.hour, now.minute) < (h, m) or store.state(c, key):
            return None
        store.state(c, key, 1)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    title, text = daily.note(c, day)
    try:
        where = knowledge.write_daily(day, title, text)
        print("daily note:", where, flush=True)
        return where
    except Exception as e:  # noqa: BLE001
        print("daily note", repr(e)[:300], flush=True)
        return None


def backup_job(c):
    """A daily copy of the mailbox under <data>/backups (team.json backup.keep, default 7; "backup": false turns it off)."""
    b = config.team().backup
    if b is False or store.state(c, "backup_day") == time.strftime("%Y-%m-%d"):
        return
    store.backup(c, keep=int((b or {}).get("keep", 7)))
    store.state(c, "backup_day", time.strftime("%Y-%m-%d"))


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
    jobs = [routines, retro, north_job, triggers, promises, digest, daily_note, backup_job]
    if config.flag("TEAM_GITHUB"):
        jobs += [pr_watches, pr_queue, merge_gate, ship.conflicts, ship.deploy_train, ship.post_deploy]
    jobs = [j for j in jobs if t.job_on(j.__name__)]   # team.json jobs_off: ["deploy_train", ...]
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
