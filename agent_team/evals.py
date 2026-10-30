"""Prompt evals: real agents on real situations, in a sandbox, before a change to how agents behave merges.

A team's prompts are code: a PR that rewrites the reviewer's instructions or the playbook can quietly break something
the team learned the hard way. With team.json `prompt_eval`, the merge gate runs the cases in `cases` (a JSON file in
the repository) on the PR's checkout and on the base branch's; a case that passes on the base and fails on the PR
blocks the merge and goes back to the PR's author with the evidence. Cases come from incidents: when the team gets
something wrong, the fix PR adds the situation as a case.

    "prompt_eval": {"config": "team/team.json", "cases": "team/evals/cases.json",
                    "paths": "^team/(prompts/|TEAM\\\\.md$|team\\\\.json$)|^REVIEW\\\\.md$",
                    "locked": "authority|approval|limit|production|money", "max_turns": 25}

- `config`: where team.json lives inside the repository (the eval loads each checkout's own).
- `paths`: a PR touching these runs the eval (only the agents whose prompt changed, all of them for shared files).
- `locked`: a changed line in those files that matches this needs a human's yes even when the eval passes (the retro
  may change prompts, never the rules a human locked).
- A case: {"id", "agent", "sender" (a member or "system"), "body", "why", "checks": [...]} plus optional "north": true
  (a north is set), "code_at": "<sha>" (runs on the code as it was, with today's prompts on top: an incident checked
  against the code that had the bug) and "score": true (an incident case: noisy, so it runs 3 times on each side and
  reports a score instead of gating; only when the reviewer's own prompt or review guide changed).
- A check: {"tool": "request_approval", "match": "regex"}, {"any": ["reply", "ask"]}, {"bash": "regex"}, or
  {"any_match": {"tools": [...], "match": "regex"}}; "absent": true inverts it.

Sandbox: a throwaway mailbox and data dir, chat, mailer and GitHub off, no tokens or keys in the environment but
Claude's own, git that can't push. `agent-team eval --root <checkout> [--base <checkout>]` runs it by hand."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import config

EVAL_NOTE = """

## TEST MODE
This run tests the team's instructions: the mailbox is a fake and git push / gh don't work here. Work exactly as you
would for real; if a command fails because of that, carry on as if it had worked."""
TEST_NORTH = {"goals": ["10 conversations with prospects by Friday", "ship the new checkout by Friday"], "why": "test",
              "until": "2099-01-01", "by": None}


def tool_uses(stream):
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    yield b["name"].split("__")[-1], b.get("input") or {}


def check(ck, uses):
    def has(names, rx=None):
        return any(n in names and (not rx or re.search(rx, json.dumps(i, ensure_ascii=False), re.I)) for n, i in uses)
    if "bash" in ck:
        ok = any(n == "Bash" and re.search(ck["bash"], i.get("command", ""), re.I) for n, i in uses)
    elif "tool" in ck:
        ok = has([ck["tool"]], ck.get("match"))
    elif "any" in ck:
        ok = has(ck["any"])
    else:
        ok = has(ck["any_match"]["tools"], ck["any_match"]["match"])
    return ok != bool(ck.get("absent"))


def sandbox_env(tmp, root, cfg):
    gitcfg = os.path.join(tmp, "gitconfig")
    with open(gitcfg, "w") as f:
        f.write("[user]\n\tname = eval\n\temail = eval@localhost\n"
                "[url \"file:///nonexistent/\"]\n\tpushInsteadOf = https://\n\tpushInsteadOf = git@\n\tpushInsteadOf = ssh://\n")
    env = {k: v for k, v in os.environ.items()   # a test run must not email, post or pay: no keys but Claude's
           if k.startswith(("ANTHROPIC_", "CLAUDE_")) or not re.search(r"TOKEN|KEY|SECRET|PASSWORD|WEBHOOK|DSN|NOTION|SLACK|DISCORD", k)}
    env.update(TEAM_EVAL="1", TMPDIR=tmp, GIT_CONFIG_GLOBAL=gitcfg, GIT_CONFIG_NOSYSTEM="1", GH_CONFIG_DIR=os.path.join(tmp, "gh"),
               GH_TOKEN="", GITHUB_TOKEN="", TEAM_CONFIG=cfg, TEAM_DATA_DIR=os.path.join(tmp, "data"),
               TEAM_DB=os.path.join(tmp, "data", "team.db"), TEAM_WORKDIR=root, TEAM_ENV_FILE=os.path.join(tmp, "none.env"),
               TEAM_MAILER_ENABLED="0", TEAM_GITHUB="0", TEAM_DISCORD_ENABLED="0", PYTHONPATH=config.ROOT)
    return env


INSTRUCTION_GLOBS = (".claude", "CLAUDE.md", "REVIEW.md")


def code_at(root, sha, tmp, team_dir):
    """A checkout of `sha` with root's instructions (the team folder, .claude/, CLAUDE.md, REVIEW.md) copied over it."""
    wt = os.path.join(tmp, "code")
    subprocess.run(["git", "-C", root, "worktree", "add", "-q", "--detach", wt, sha], check=True, capture_output=True)
    for rel in INSTRUCTION_GLOBS + (team_dir,):
        src, dst = os.path.join(root, rel), os.path.join(wt, rel)
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif os.path.exists(src):
            shutil.copy2(src, dst)
    return wt


def run_case(root, case, spec, timeout=600):
    """One case on one checkout: the agent runs once, headless, in the sandbox. Returns the result dict."""
    with tempfile.TemporaryDirectory(prefix="agent-team-eval-") as tmp:
        rel_cfg = spec.get("config", "team.json")
        cwd = code_at(root, case["code_at"], tmp, os.path.dirname(rel_cfg) or ".") if case.get("code_at") else root
        env = sandbox_env(tmp, cwd, os.path.join(cwd, rel_cfg))
        t0 = time.time()
        try:
            out = subprocess.run([sys.executable, "-m", "agent_team", "eval-case", "--case", json.dumps(case),
                                  "--max-turns", str(spec.get("max_turns", 25))],
                                 cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        if cwd != root:
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force", cwd], capture_output=True)
    uses = list(tool_uses(out))
    failed = [ck for ck in case["checks"] if not check(ck, uses)]
    return {"id": case["id"], "agent": case["agent"], "ok": not failed, "failed": failed, "secs": int(time.time() - t0),
            "tools": [n for n, _ in uses][:40],
            "calls": [[n, json.dumps(i, ensure_ascii=False)[:400]] for n, i in uses if n not in ("Read", "Glob", "Grep", "ToolSearch")][:30]}


def eval_case(case, max_turns=25):
    """Inside the sandbox (`agent-team eval-case`): seed the mailbox, run the agent, stream Claude's output to stdout."""
    from . import dispatcher, store
    t = config.team()
    c = store.db()
    if case.get("north"):
        store.state(c, "north", json.dumps(dict(TEST_NORTH, by=t.orchestrator, at=time.time())))
    body = case["body"]
    if "{gate}" in body:
        goals = dispatcher.north(c)[0]
        body = body.replace("{gate}", dispatcher.NORTH_GATE.format(goals=goals) if goals else "")
    store.send(c, case.get("sender", "system"), case["agent"], "task" if case.get("sender", "system") == "system" else "ask", body, mirror=False)
    cmd = dispatcher.command(case["agent"], dispatcher.run_prompt(case["agent"], c) + EVAL_NOTE) + ["--max-turns", str(max_turns),
                                                                                                   "--strict-mcp-config"]
    p = subprocess.run(cmd, cwd=config.workdir(), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    sys.stdout.write(p.stdout)


def load_cases(root, base, spec):
    """The base's cases when it has them: a PR can't weaken its own test."""
    rel = spec.get("cases", "evals/cases.json")
    src = base if base and os.path.exists(os.path.join(base, rel)) else root
    with open(os.path.join(src, rel)) as f:
        return json.load(f)


def run(root, base=None, agents=None, score=False, spec=None, log=print):
    spec = spec or (config.team().prompt_eval or {})
    cases = load_cases(root, base, spec)
    if agents:
        cases = [k for k in cases if k["agent"] in agents]
    res = []
    for k in cases:
        if k.get("score"):
            if score:
                rs = [run_case(root, k, spec) for _ in range(3)]
                r = dict(rs[-1], ok=all(x["ok"] for x in rs), hits=sum(x["ok"] for x in rs), runs=3, regression=False)
                if base:
                    r["base_hits"] = sum(run_case(base, k, spec)["ok"] for _ in range(3))
                log("score %-26s %-10s PR %d/3%s" % (k["id"], k["agent"], r["hits"], ", base %d/3" % r["base_hits"] if base else ""))
                res.append(r)
            continue
        r = run_case(root, k, spec)
        if base:
            if not r["ok"]:   # one retry: a single run is noisy
                r = run_case(root, k, spec)
            b = run_case(base, k, spec) if not r["ok"] else {"ok": None}
            r["base_ok"], r["regression"] = b["ok"], not r["ok"] and bool(b["ok"])
        log("%s %-26s %-10s %3ds %s" % ("ok  " if r["ok"] else ("FAIL" if r.get("regression", True) else "same"), k["id"], k["agent"],
                                         r["secs"], "" if r["ok"] else json.dumps(r["failed"], ensure_ascii=False)))
        res.append(r)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-team eval")
    ap.add_argument("--root", required=True, help="checkout to test")
    ap.add_argument("--base", help="checkout of the base branch: only regressions against it fail")
    ap.add_argument("--agents", help="comma separated")
    ap.add_argument("--json", help="write the results here")
    ap.add_argument("--score", action="store_true", help="also run the score (incident) cases, 3 times each")
    a = ap.parse_args(argv)
    res = run(os.path.abspath(a.root), a.base and os.path.abspath(a.base), a.agents and a.agents.split(","), a.score)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    gate = [r for r in res if "hits" not in r]
    bad = [r for r in gate if (r.get("regression") if a.base else not r["ok"])]
    print("%d/%d ok%s" % (sum(r["ok"] for r in gate), len(gate), ", %d regression(s)" % len(bad) if a.base else ""))
    return 1 if bad else 0
