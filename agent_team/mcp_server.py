"""MCP server `team` (stdio, JSON-RPC, standard library only): how agents, and the humans' own Claude sessions, talk to
each other. Run as: python -m agent_team mcp --as <member>.

The tool list is built from team.json, so the `to` fields only accept real teammates."""
import json
import os
import re
import subprocess
import sys
import time

from . import config, store

CURRENT = None   # the conversation this run is handling (set by inbox): what the agent sends stays in it
ME = None


def tools():
    t = config.team()
    members = t.members
    files = {"type": "array", "items": {"type": "string"}, "maxItems": 10,
             "description": "Files to send with it (absolute paths: a report, CSV, screenshot, PDF), up to 10 MB each."}
    about = {"type": "integer", "description": "Rarely needed: what you send already stays in the conversation you're "
                                               "handling. Use it only to attach this to a different conversation (that message's id)."}
    humans = ", ".join(t.display(h) for h in t.humans)
    share = {"name": "share", "description": "Tell the humans (%s) something they'd want to know: something shipped, a finding, a result. Not progress notes. One short message." % humans,
             "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "maxLength": 3000}, "files": files}}}
    if t.digest:
        share["description"] = ("Something the humans (%s) would want to know: shipped, a finding, a result. By default it goes in the daily digest "
                                "at %02d:%02d (not a message now). `urgent` only for what can't wait (the top authority level): a customer hurt now, "
                                "security, something down. One short message." % ((humans,) + t.at(t.digest)))
        share["inputSchema"]["properties"]["urgent"] = {"type": "boolean", "description": "Tell the humans now"}
    out = [
        {"name": "ask", "description": "Ask a teammate a question. Returns at once with the message id; the answer arrives in your inbox later (you are woken up for it). Don't wait or poll: carry on, or finish your turn.",
         "inputSchema": {"type": "object", "required": ["to", "question"], "properties": {
             "to": {"type": "string", "enum": members}, "question": {"type": "string", "maxLength": 6000}, "about": about, "files": files}}},
        {"name": "assign", "description": "Give a teammate a task (an outcome, with the context they need and what done looks like). They are woken up for it and reply when done.",
         "inputSchema": {"type": "object", "required": ["to", "task"], "properties": {
             "to": {"type": "string", "enum": members}, "task": {"type": "string", "maxLength": 8000}, "about": about, "files": files}}},
        {"name": "reply", "description": "Answer a message from your inbox (the sender is woken up with it). Mark tasks done by replying with the result.",
         "inputSchema": {"type": "object", "required": ["message_id", "answer"], "properties": {
             "message_id": {"type": "integer"}, "answer": {"type": "string", "maxLength": 8000}, "files": files}}},
        {"name": "inbox", "description": "Your unread messages of one conversation (questions, tasks, answers), with the conversation so far. Reading marks them read.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "thread", "description": "The whole conversation a message belongs to.",
         "inputSchema": {"type": "object", "required": ["message_id"], "properties": {"message_id": {"type": "integer"}}}},
        {"name": "team_log", "description": "The latest messages across the team (or one member's), oldest first.",
         "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                                         "agent": {"type": "string", "enum": members}}}},
        {"name": "post_update", "description": "A short status note for the orchestrator's log (what you did, what's next, what blocks you). Not shown to the humans.",
         "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "maxLength": 3000}}}},
        share,
        {"name": "request_approval", "description": "Ask the humans before anything that needs them (see %s in your instructions; check the standing decisions first: if one covers it, act on it). Say what will happen, the options and your recommendation. It opens a decision; %s; don't act before it." % (
             "the authority levels" if t.authority else "the approval rules",
             "the orchestrator resolves the humans' answer and tells you" if t.single_door else "their answer arrives in your inbox"),
         "inputSchema": {"type": "object", "required": ["what"], "properties": {"what": {"type": "string", "maxLength": 4000}, "files": files,
             "kind": {"type": "string", "enum": t.decision_kinds, "description": "What it is about (default other)"},
             "pr": {"type": "integer", "description": "kind pr: the PR number (the merge gate waits for this decision)"}}}},
        {"name": "remember", "description": "Save a human's decision or standing preference in the team's memory, so nobody asks it again. One line with the why; not progress notes. `precedent`: the decision settles a whole category from now on (\"a discount up to 15% → level 1\").",
         "inputSchema": {"type": "object", "required": ["decision"], "properties": {"decision": {"type": "string", "maxLength": 600},
                                                                                   "who": {"type": "string", "enum": list(t.humans)},
                                                                                   "precedent": {"type": "boolean"}}}},
        {"name": "commit", "description": "Register a promise: anything you'll do after this run ends (\"I'll update the page after the deploy\", \"the report this afternoon\"). The dispatcher wakes you in this conversation when `depends` closes or at `due`, whichever comes first; an open promise long past due wakes the orchestrator. Close it with commit_done or commit_failed.",
         "inputSchema": {"type": "object", "required": ["what", "due"], "properties": {"what": {"type": "string", "maxLength": 500},
             "due": {"type": "string", "description": "When: \"14:00\" (today, or tomorrow if past), \"+90m\", \"+2h\", \"2026-10-22 07:00\""},
             "depends": {"type": "string", "description": "Optional: pr:<number> (merged) or decision:<id> (decided)"}}}},
        {"name": "commit_done", "description": "Close a promise you made, with the proof (PR link, what's live, the number).",
         "inputSchema": {"type": "object", "required": ["id", "proof"], "properties": {"id": {"type": "integer"}, "proof": {"type": "string", "maxLength": 1000}}}},
        {"name": "commit_failed", "description": "Close a promise you couldn't keep, with why and what happens instead.",
         "inputSchema": {"type": "object", "required": ["id", "why"], "properties": {"id": {"type": "integer"}, "why": {"type": "string", "maxLength": 1000}}}},
        {"name": "teammates", "description": "Who is on the team and what each one does.", "inputSchema": {"type": "object", "properties": {}}},
    ]
    if may_decide(ME):
        out.append({"name": "decide", "description": ("Record a human's answer to a pending decision (ids in your inbox, \"Waiting for a human\"). "
                    "Only after a human said it in that conversation; you never decide a decision yourself. Emails aren't decisions: the human's own message approves them."
                    if not t.is_human(ME) else "Approve or reject a pending decision (see `decisions`)."),
                    "inputSchema": {"type": "object", "required": ["decision", "verdict"], "properties": {"decision": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["approved", "rejected"]}, "human": {"type": "string", "enum": list(t.humans)},
                        "note": {"type": "string", "maxLength": 1000, "description": "What the human added (a change, a condition), passed to the owner"}}}})
        out.append({"name": "decisions", "description": "The decisions waiting for a human, oldest first.", "inputSchema": {"type": "object", "properties": {}}})
    if config.flag("TEAM_MAILER_ENABLED"):
        out += [
            {"name": "propose_email", "description": "Propose an email. A human must approve it before the mailer sends it (you never send it yourself). Plain text. Check the outcome later with `outbox`.",
             "inputSchema": {"type": "object", "required": ["to", "subject", "body"], "properties": {
                 "to": {"type": "string", "description": "One address"}, "subject": {"type": "string", "maxLength": 200},
                 "body": {"type": "string", "maxLength": 4000}, "campaign": {"type": "string", "maxLength": 60}}}},
            {"name": "outbox", "description": "Your proposed emails and their status: pending, approved, sent, failed, rejected.",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
    if config.flag("TEAM_GITHUB") and t.merge_gate is not None and ME == t.reviewer:
        out.append({"name": "review", "description": "Your verdict on a PR after reading the diff. ok: the merge gate merges it by itself once CI is green on this same commit, no decision is pending and no deploy runs. block: nothing merges; also `assign` the author the numbered list. needs_human: a change the authority levels give to a human, not asked by one in this conversation: opens the decision.",
                    "inputSchema": {"type": "object", "required": ["pr", "verdict"], "properties": {"pr": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["ok", "block"]}, "needs_human": {"type": "boolean"},
                        "what": {"type": "string", "maxLength": 600, "description": "needs_human: two lines for the humans on what changes"}}}})
    if config.flag("TEAM_GITHUB"):
        out.append({"name": "wait_for_pr", "description": "Instead of waiting for a PR's CI inside your run: call this and finish. The dispatcher watches the checks and wakes you in this same conversation when they pass or fail, with your `then` note.",
                    "inputSchema": {"type": "object", "required": ["pr", "then"], "properties": {
                        "pr": {"type": "integer"}, "then": {"type": "string", "maxLength": 1000}}}})
    return out


def may_decide(who):
    """Humans always; with a single door only the orchestrator relays their answers; otherwise the decision's owner."""
    t = config.team()
    return bool(who) and (t.is_human(who) or (who == t.orchestrator if t.single_door else who in t.roles))


def guard_promise(c, text):
    """With promises required (team.json promises.require, on with a single door), "I'll let you know when…" needs a `commit`."""
    t = config.team()
    if t.promises.get("require", t.single_door):
        store.promised(c, ME, CURRENT, text)


def gh_pr(n):
    out = subprocess.run(["gh", "pr", "view", str(n), "--json", "headRefOid,state,title,url"]
                         + (["-R", config.setting("TEAM_GITHUB_REPO")] if config.setting("TEAM_GITHUB_REPO") else []),
                         cwd=config.workdir(), capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise ValueError("Can't read PR #%s: %s" % (n, out.stderr[:200]))
    return json.loads(out.stdout)


def short_enough(text):
    t = config.team()
    if len(text) > t.human_max:
        raise ValueError("Too long for a human (%d characters, max %d). Rewrite it: the conclusion or the question first, "
                         "3 to 5 short lines. Put any detail in a file or PR and link it." % (len(text), t.human_max))


def memory_path():
    return config.setting("TEAM_MEMORY_FILE", os.path.join(config.data_dir(), "MEMORY.md"))


def remember(decision, who, precedent=False):
    """One dated line at the top of the memory file (newest first). Put in every agent's prompt by the dispatcher.
    A precedent settles a whole category ("discounts up to 15% → level 1"): the orchestrator checks them before asking."""
    path = memory_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "- %s · %s%s (%s, via %s)" % (time.strftime("%Y-%m-%d"), "Precedent: " if precedent else "", " ".join(decision.split()),
                                        config.team().display(who), ME)
    try:
        with open(path) as f:
            old = f.read()
    except OSError:
        old = ""
    head = "# Team memory: standing decisions\n\n"
    body = old[len(head):] if old.startswith(head) else old
    with open(path, "w") as f:
        f.write(head + line + "\n" + body)
    return "Saved: " + line


def call(name, a):
    global CURRENT
    t = config.team()
    c = store.db()
    if name in ("ask", "assign"):
        th = CURRENT
        if a.get("about"):
            r = c.execute("SELECT thread, id FROM messages WHERE id = ?", (a["about"],)).fetchone()
            th = (r["thread"] or r["id"]) if r else None
        text = a.get("question") or a.get("task")
        if t.is_human(a["to"]) and not t.is_human(ME):
            short_enough(text)
        if t.is_human(a["to"]) or a["to"] == t.orchestrator:
            guard_promise(c, text)
        mid = store.send(c, ME, a["to"], "ask" if name == "ask" else "task", text, thread=th, files=store.outgoing(a.get("files")))
        return "Sent (#%d). The answer arrives in your inbox." % mid
    if name == "reply":
        files = store.outgoing(a.get("files"))
        m = c.execute("SELECT sender, thread FROM messages WHERE id = ?", (a["message_id"],)).fetchone()
        if not m:
            raise ValueError("No message %s" % a["message_id"])
        c.execute("UPDATE messages SET status = 'done' WHERE id = ?", (a["message_id"],))
        if m["sender"] == ME:
            return "That message is yours; nothing sent."
        if m["sender"] == "system":
            return "Marked done. It was an automatic task: use post_update for the log, or share if the humans should know."
        if t.is_human(m["sender"]) and not t.is_human(ME):
            short_enough(a["answer"])
        guard_promise(c, a["answer"])
        return "Replied as #%d." % store.send(c, ME, m["sender"], "reply", a["answer"], reply_to=a["message_id"], files=files)
    if name == "inbox":
        out = []
        for m in store.inbox(c, ME):
            CURRENT = m["thread"] or m["id"]
            root = c.execute("SELECT body FROM messages WHERE id = ?", (CURRENT,)).fetchone()
            store.state(c, "thread:" + ME, CURRENT)
            store.state(c, "subject:" + ME, " ".join((root["body"] if root else m["body"]).split())[:160])
            before = [r for r in store.thread(c, CURRENT) if r["id"] < m["id"] and r["kind"] != "update"][-10:]
            ctx = "\n".join("  %s: %s" % (r["sender"], " ".join(r["body"].split())[:700]) for r in before)
            waiting = store.pending_text(c, CURRENT)
            mine = "\n".join("- promise %d (due %s): %s" % (p["id"], time.strftime("%d/%m %H:%M", time.localtime(p["due"])), p["what"])
                             for p in c.execute("SELECT * FROM promises WHERE agent = ? AND status = 'open' AND thread = ?", (ME, CURRENT)))
            out.append(store.fmt([m]) + ("\n[The conversation so far, oldest first:]\n" + ctx if ctx else "")
                       + ("\n[Waiting for a human in this conversation:]\n" + waiting if waiting else "")
                       + ("\n[Your open promises in this conversation:]\n" + mine if mine else ""))
        return "\n\n---\n\n".join(out) or "Nothing."
    if name == "thread":
        return store.fmt(store.thread(c, a["message_id"]))
    if name == "team_log":
        return store.fmt(store.log(c, a.get("limit", 30), a.get("agent")))
    if name == "post_update":
        c.execute("INSERT INTO messages (sender, recipient, kind, body, status, created_at) VALUES (?, ?, 'update', ?, 'done', ?)",
                  (ME, t.orchestrator, a["text"], time.time()))
        return "Posted."
    if name == "share":
        short_enough(a["text"])
        guard_promise(c, a["text"])
        files = store.outgoing(a.get("files"))
        dec, mails = store.pending(c, CURRENT) if CURRENT else ([], [])
        if not t.digest or a.get("urgent") or ME == t.orchestrator or t.is_human(ME) or dec or mails:
            # now: no digest, can't wait, or context for what the humans must decide in this conversation
            store.send(c, ME, t.primary_human, "update", a["text"], thread=CURRENT, files=files)
            return "Shared with the humans."
        c.execute("INSERT INTO messages (thread, sender, recipient, kind, body, status, created_at) VALUES (?, ?, ?, 'share', ?, 'done', ?)",
                  (CURRENT, ME, t.orchestrator, a["text"] + store.attached(files), time.time()))
        return "Saved for today's %02d:%02d digest (not urgent). Use urgent=true only for what can't wait." % t.at(t.digest)
    if name == "request_approval":
        short_enough(a["what"])
        kind = a.get("kind") or "other"
        if kind not in t.decision_kinds:
            raise ValueError("kind is one of: %s" % ", ".join(t.decision_kinds))
        if kind == "pr" and not a.get("pr"):
            raise ValueError("kind pr needs `pr` (the number): the merge gate waits for this decision.")
        did, mid = store.open_decision(c, ME, kind, a["what"], thread=CURRENT, ref="pr:%d" % a["pr"] if a.get("pr") else None,
                                       files=store.outgoing(a.get("files")))
        if mid is None:
            return "Decision %d for PR #%d is already waiting for a human; nothing sent." % (did, a["pr"])
        return ("Decision %d opened for the humans. Stop here: %s%s." % (did,
                "the orchestrator tells you their answer" if t.single_door else "their answer arrives in your inbox",
                " (the merge gate merges on approval)" if kind == "pr" and t.merge_gate is not None else ""))
    if name == "decide" and may_decide(ME):
        d = c.execute("SELECT * FROM decisions WHERE id = ?", (a["decision"],)).fetchone()
        if not d or d["status"] != "pending":
            raise ValueError("Decision %s isn't pending." % a["decision"])
        if not t.is_human(ME) and not t.single_door and d["owner"] != ME:
            raise ValueError("Decision %d is %s's to relay, not yours." % (d["id"], d["owner"]))
        by = ME if t.is_human(ME) else a.get("human")
        if not t.is_human(by):
            raise ValueError("Say which human answered (`human`).")
        if by not in t.approvers():
            raise ValueError("%s doesn't approve things on this team (approvals.approvers)." % t.display(by))
        if not t.is_human(ME) and not c.execute("SELECT 1 FROM messages WHERE thread = ? AND sender = ? AND created_at > ?",
                                                (d["thread"], by, d["created_at"])).fetchone():
            raise ValueError("%s hasn't answered in that conversation since the decision was opened." % t.display(by))
        store.decide(c, d["id"], a["verdict"], by, note=a.get("note"), sender=ME)
        if d["kind"] == "pr" and a["verdict"] == "approved" and t.merge_gate is not None:
            return "Decision %d approved: the merge gate merges %s once the review is ok and CI is green." % (d["id"], d["ref"])
        return "Decision %d %s; %s was told." % (d["id"], a["verdict"], d["owner"])
    if name == "decisions" and may_decide(ME):
        rows = c.execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY id").fetchall()
        return "\n".join("%d · %s%s · %s · %s" % (r["id"], r["kind"], " " + r["ref"] if r["ref"] else "", r["owner"],
                                                  " ".join(r["what"].split())[:200]) for r in rows) or "Nothing waits for a human."
    if name == "commit":
        dep = (a.get("depends") or "").strip() or None
        if dep and not re.fullmatch(r"(pr|decision):\d+", dep):
            raise ValueError("`depends` is pr:<number> or decision:<id>.")
        pid = c.execute("INSERT INTO promises (agent, thread, what, due, depends, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (ME, CURRENT, a["what"], store.due_at(a["due"]), dep, time.time())).lastrowid
        return "Promise %d registered. Finish your run; you're woken here when it's time." % pid
    if name in ("commit_done", "commit_failed"):
        n = c.execute("UPDATE promises SET status = ?, proof = ?, closed_at = ? WHERE id = ? AND agent = ? AND status = 'open'",
                      ("done" if name == "commit_done" else "failed", a.get("proof") or a.get("why"), time.time(), a["id"], ME)).rowcount
        if not n:
            raise ValueError("No open promise %s of yours." % a["id"])
        p = c.execute("SELECT * FROM promises WHERE id = ?", (a["id"],)).fetchone()
        store.did(c, ME, ("" if name == "commit_done" else "Couldn't: ") + p["what"], proof=p["proof"], thread=p["thread"])
        return "Promise %d closed. Now tell whoever asked (reply), with the proof." % a["id"]
    if name == "review" and ME == t.reviewer and t.merge_gate is not None and config.flag("TEAM_GITHUB"):
        info = gh_pr(a["pr"])
        if info["state"] != "OPEN":
            return "PR #%d is %s; nothing to do." % (a["pr"], info["state"].lower())
        store.state(c, "verdict:%d" % a["pr"], json.dumps({"head": info["headRefOid"], "verdict": a["verdict"], "thread": CURRENT, "at": time.time()}))
        if a["verdict"] == "ok" and a.get("needs_human"):
            if not a.get("what"):
                raise ValueError("needs_human: write `what` (two lines on what changes).")
            store.open_decision(c, ME, "pr", "PR #%d (%s): %s\n%s" % (a["pr"], info["title"], a["what"], info.get("url", "")),
                                thread=CURRENT, ref="pr:%d" % a["pr"])
            return "Review saved; decision opened. The gate merges after a human approves. Finish your run."
        return ("Review saved: the gate merges it when CI is green on this commit and no deploy runs. Finish your run." if a["verdict"] == "ok"
                else "Blocked: nothing merges. `assign` the author the numbered list, then finish.")
    if name == "propose_email" and config.flag("TEAM_MAILER_ENABLED"):
        to = a["to"].strip()
        if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[a-z]{2,}", to, re.I):
            raise ValueError("One plain address in `to`, like name@example.com.")
        code = store.email_code(to, a["subject"], a["body"])
        oid = c.execute("INSERT INTO outbox (agent, thread, to_email, subject, body, campaign, code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ME, CURRENT, to, a["subject"].strip(), a["body"].strip(), (a.get("campaign") or "").strip() or None, code, time.time())).lastrowid
        text = ("Email to approve (no. %d, code %s)\nTo: %s\nSubject: %s\n\n%s\n\nApprove with `agent-team approve %d`%s."
                % (oid, code, to, a["subject"].strip(), a["body"].strip(), oid,
                   " or answer \"approve\" in this chat thread" if config.flag("TEAM_DISCORD_ENABLED") else ""))
        mid = store.send(c, ME, t.approvers()[0], "approval", text, thread=CURRENT)
        if CURRENT is None:
            c.execute("UPDATE outbox SET thread = ? WHERE id = ?", (mid, oid))
        return "Proposed as email %d. When a human approves it, the mailer sends it; see `outbox`." % oid
    if name == "outbox" and config.flag("TEAM_MAILER_ENABLED"):
        rows = c.execute("SELECT * FROM outbox WHERE agent = ? ORDER BY id DESC LIMIT 30", (ME,)).fetchall()
        return "\n".join("%d · %s · %s · %s%s" % (r["id"], r["status"], r["to_email"], r["subject"][:60],
                                                   (" · " + r["error"][:120]) if r["error"] else "") for r in rows) or "Nothing proposed yet."
    if name == "remember":
        return remember(a["decision"], a.get("who") or t.primary_human, bool(a.get("precedent")))
    if name == "wait_for_pr" and config.flag("TEAM_GITHUB"):
        if ME == t.reviewer and t.merge_gate is not None:
            return "Not needed: every PR comes back to you when its CI finishes, and the gate merges after your review. Finish your run."
        store.state(c, "prwatch:%d:%s" % (a["pr"], ME), json.dumps({"thread": CURRENT, "then": a["then"], "at": time.time()}))
        return "Watching PR #%d. Finish your run now (post_update first); you'll be woken here when its checks are done." % a["pr"]
    if name == "teammates":
        return "\n".join("%s: %s%s" % (m, t.summary(m), " (human)" if t.is_human(m) else "") for m in t.members)
    raise ValueError("Unknown tool %s" % name)


def handle(msg):
    """One JSON-RPC request → the response dict (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    t = config.team()
    if method == "initialize":
        res = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
               "serverInfo": {"name": "team", "version": "0.1.0"},
               "instructions": "You are '%s' on the %s team. Teammates: %s. Ask and assign through these tools; never wait "
                               "for an answer, you are woken up when it arrives." % (ME, t.name, ", ".join(t.members))}
    elif method == "tools/list":
        res = {"tools": tools()}
    elif method == "tools/call":
        try:
            res = {"content": [{"type": "text", "text": call(params.get("name"), params.get("arguments") or {})}]}
        except Exception as e:  # noqa: BLE001
            res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    elif method == "ping":
        res = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Unknown method"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main(me):
    global ME
    t = config.team()
    if me not in t.members:
        raise SystemExit("--as must be one of: %s" % ", ".join(t.members))
    ME = me
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
