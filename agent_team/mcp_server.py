"""MCP server `team` (stdio, JSON-RPC, standard library only): how agents, and the humans' own Claude sessions, talk to
each other. Run as: python -m agent_team mcp --as <member>.

The tool list is built from team.json, so the `to` fields only accept real teammates."""
import json
import os
import re
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
        {"name": "share", "description": "Tell the humans (%s) something they'd want to know: something shipped, a finding, a decision you need soon. Not progress notes. One short message." % humans,
         "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "maxLength": 3000}, "files": files}}},
        {"name": "request_approval", "description": "Ask the humans before anything that needs them (see the approval rules in your instructions). Describe exactly what will happen. Their answer arrives in your inbox; don't act before it.",
         "inputSchema": {"type": "object", "required": ["what"], "properties": {"what": {"type": "string", "maxLength": 4000}, "files": files}}},
        {"name": "remember", "description": "Save a human's decision or standing preference in the team's memory, so nobody asks it again. One line with the why; not progress notes.",
         "inputSchema": {"type": "object", "required": ["decision"], "properties": {"decision": {"type": "string", "maxLength": 600},
                                                                                   "who": {"type": "string", "enum": list(t.humans)}}}},
        {"name": "teammates", "description": "Who is on the team and what each one does.", "inputSchema": {"type": "object", "properties": {}}},
    ]
    if config.flag("TEAM_MAILER_ENABLED"):
        out += [
            {"name": "propose_email", "description": "Propose an email. A human must approve it before the mailer sends it (you never send it yourself). Plain text. Check the outcome later with `outbox`.",
             "inputSchema": {"type": "object", "required": ["to", "subject", "body"], "properties": {
                 "to": {"type": "string", "description": "One address"}, "subject": {"type": "string", "maxLength": 200},
                 "body": {"type": "string", "maxLength": 4000}, "campaign": {"type": "string", "maxLength": 60}}}},
            {"name": "outbox", "description": "Your proposed emails and their status: pending, approved, sent, failed, rejected.",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
    if config.flag("TEAM_GITHUB"):
        out.append({"name": "wait_for_pr", "description": "Instead of waiting for a PR's CI inside your run: call this and finish. The dispatcher watches the checks and wakes you in this same conversation when they pass or fail, with your `then` note.",
                    "inputSchema": {"type": "object", "required": ["pr", "then"], "properties": {
                        "pr": {"type": "integer"}, "then": {"type": "string", "maxLength": 1000}}}})
    return out


def short_enough(text):
    t = config.team()
    if len(text) > t.human_max:
        raise ValueError("Too long for a human (%d characters, max %d). Rewrite it: the conclusion or the question first, "
                         "3 to 5 short lines. Put any detail in a file or PR and link it." % (len(text), t.human_max))


def memory_path():
    return config.setting("TEAM_MEMORY_FILE", os.path.join(config.data_dir(), "MEMORY.md"))


def remember(decision, who):
    """One dated line at the top of the memory file (newest first). Put in every agent's prompt by the dispatcher."""
    path = memory_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "- %s · %s (%s, via %s)" % (time.strftime("%Y-%m-%d"), " ".join(decision.split()), config.team().display(who), ME)
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
            out.append(store.fmt([m]) + ("\n[The conversation so far, oldest first:]\n" + ctx if ctx else ""))
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
        store.send(c, ME, t.primary_human, "update", a["text"], thread=CURRENT, files=store.outgoing(a.get("files")))
        return "Shared with the humans."
    if name == "request_approval":
        short_enough(a["what"])
        mid = store.send(c, ME, t.approvers()[0], "approval", a["what"], thread=CURRENT, files=store.outgoing(a.get("files")))
        return "Approval #%d requested. Don't do it until the answer arrives in your inbox." % mid
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
        return remember(a["decision"], a.get("who") or t.primary_human)
    if name == "wait_for_pr" and config.flag("TEAM_GITHUB"):
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
