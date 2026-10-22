"""agent-team command line. Run `python -m agent_team --help`."""
import argparse
import json
import sys
import time

from . import config, store


def cmd_check(a):
    t = config.team()
    print("team: %s (%s)" % (t.name, t.path))
    print("agents: %s" % ", ".join(t.roles))
    print("humans: %s (approvers: %s)" % (", ".join(t.humans), ", ".join(t.approvers())))
    print("orchestrator: %s · reviewer: %s" % (t.orchestrator, t.reviewer or "-"))
    print("routines: %d · triggers: %d · heartbeat: %s" % (len(t.routines), len(t.triggers),
                                                         (t.heartbeat.get("when", "always") if t.heartbeat else "off")))
    print("single door: %s · authority levels: %d · digest: %s · promises required: %s · merge gate: %s · retro: %s" % (
        "on" if t.single_door else "off", len(t.authority.get("levels") or []),
        "%02d:%02d" % t.at(t.digest) if t.digest else "off", "on" if t.promises.get("require", t.single_door) else "off",
        "on" if t.merge_gate is not None else "off", t.retro["agent"] if t.retro else "off"))
    print("database: %s" % config.db_path())
    print("dry run: %s · discord: %s · mailer: %s · github: %s · permission mode: %s" % tuple(
        ["on" if config.flag(k) else "off" for k in ("TEAM_DRY_RUN", "TEAM_DISCORD_ENABLED", "TEAM_MAILER_ENABLED", "TEAM_GITHUB")]
        + [config.setting("TEAM_PERMISSION_MODE", "default")]))
    print("ok")


def cmd_init(a):
    store.db()
    print("mailbox ready at", config.db_path())


def cmd_mcp(a):
    from . import mcp_server
    mcp_server.main(a.as_)


def cmd_dispatcher(a):
    from . import dispatcher
    dispatcher.main()


def cmd_office(a):
    from . import office
    server = office.serve(a.host, a.port, a.replay, a.speed)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_send(a):
    t = config.team()
    sender = a.from_ or t.primary_human
    to = a.to or t.orchestrator
    mid = store.send(store.db(), sender, to, "ask" if a.text.rstrip().endswith("?") else "task", a.text, mirror=False,
                     reply_to=a.reply_to)
    print("sent #%d from %s to %s" % (mid, sender, to))


def cmd_log(a):
    print(store.fmt(store.log(store.db(), a.limit, a.agent)))


def cmd_inbox(a):
    t = config.team()
    print(store.fmt(store.inbox(store.db(), a.as_ or t.primary_human, mark=not a.peek)))


def cmd_approve(a):
    secret = config.setting("TEAM_APPROVAL_SECRET")
    if not secret:
        raise SystemExit("Set TEAM_APPROVAL_SECRET (only humans and the mailer should be able to read it).")
    c = store.db()
    row = c.execute("SELECT * FROM outbox WHERE id = ?", (a.id,)).fetchone()
    if not row or row["status"] != "pending":
        raise SystemExit("No pending email %s." % a.id)
    print("To: %s\nSubject: %s\n\n%s\n" % (row["to_email"], row["subject"], row["body"]))
    if not a.yes and input("Send this email? [y/N] ").strip().lower() != "y":
        raise SystemExit("Not approved.")
    c.execute("UPDATE outbox SET status = 'approved', approved_by = 'cli', approval_ref = ? WHERE id = ?",
              (store.approval_sig(secret, row["id"], row["code"]), row["id"]))
    print("approved #%d" % row["id"])


def cmd_reject(a):
    store.db().execute("UPDATE outbox SET status = 'rejected', error = 'rejected by a human' WHERE id = ? AND status = 'pending'", (a.id,))
    print("rejected #%d" % a.id)


def cmd_decisions(a):
    rows = store.db().execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY id").fetchall()
    print("\n".join("%d · %s%s · %s · %s" % (r["id"], r["kind"], " " + r["ref"] if r["ref"] else "", r["owner"],
                                            " ".join(r["what"].split())[:160]) for r in rows) or "Nothing waits for a human.")


def cmd_decide(a):
    t = config.team()
    who = a.as_ or t.primary_human
    if who not in t.approvers():
        raise SystemExit("%s is not an approver (approvals.approvers in team.json)." % who)
    try:
        d = store.decide(store.db(), a.id, {"approve": "approved", "reject": "rejected"}[a.verdict], who, note=a.note)
    except ValueError as e:
        raise SystemExit(str(e))
    print("decision %d %s by %s; %s was told" % (d["id"], d["status"], who, d["owner"]))


def cmd_promises(a):
    rows = store.db().execute("SELECT * FROM promises WHERE status = 'open' ORDER BY due").fetchall()
    print("\n".join("%d · %s · due %s%s · %s" % (r["id"], r["agent"], time.strftime("%d/%m %H:%M", time.localtime(r["due"])),
                                                 " · after " + r["depends"] if r["depends"] else "", r["what"]) for r in rows)
          or "No open promises.")


def cmd_digest(a):
    from . import dispatcher
    print(dispatcher.digest_text(store.db()) or "Nothing happened today.")


def cmd_stop(a):
    store.state(store.db(), "stopped", "1")
    print("team stopped (no new runs)")


def cmd_resume(a):
    c = store.db()
    store.state(c, "stopped", "0")
    store.state(c, "paused_until", 0)
    print("team resumed")


def cmd_mcp_config(a):
    """The MCP config a human's own Claude Code session uses to join the team (e.g. `claude mcp add-json`)."""
    t = config.team()
    who = a.as_ or t.primary_human
    print(json.dumps({"type": "stdio", "command": sys.executable, "args": ["-m", "agent_team", "mcp", "--as", who],
                      "env": {"TEAM_CONFIG": t.path, "TEAM_DB": config.db_path(), "PYTHONPATH": config.ROOT}}, indent=2))


def cmd_prompt(a):
    from . import dispatcher
    print(dispatcher.system_prompt(a.agent))


def cmd_discord(a):
    from . import discord_bot
    discord_bot.main()


def cmd_mailer(a):
    from . import mailer
    mailer.main()


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent-team", description="A team of Claude Code agents that talk through a shared mailbox.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate team.json and show the settings").set_defaults(f=cmd_check)
    sub.add_parser("init", help="create the mailbox database").set_defaults(f=cmd_init)
    s = sub.add_parser("mcp", help="run the `team` MCP server (stdio) as one member")
    s.add_argument("--as", dest="as_", required=True)
    s.set_defaults(f=cmd_mcp)
    sub.add_parser("dispatcher", help="wake agents when they have mail (TEAM_DRY_RUN=1 to try it without Claude)").set_defaults(f=cmd_dispatcher)
    s = sub.add_parser("office", help="serve the 3D office")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--replay", help="play a scripted conversation (JSON) instead of the real mailbox")
    s.add_argument("--speed", type=float, default=1.0)
    s.set_defaults(f=cmd_office)
    s = sub.add_parser("send", help="send a message as a human (default: the first human, to the orchestrator)")
    s.add_argument("text")
    s.add_argument("--to")
    s.add_argument("--from", dest="from_")
    s.add_argument("--reply-to", type=int)
    s.set_defaults(f=cmd_send)
    s = sub.add_parser("log", help="latest messages")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--agent")
    s.set_defaults(f=cmd_log)
    s = sub.add_parser("inbox", help="the humans' unread messages")
    s.add_argument("--as", dest="as_")
    s.add_argument("--peek", action="store_true", help="don't mark them read")
    s.set_defaults(f=cmd_inbox)
    s = sub.add_parser("approve", help="approve a proposed email (signs it with TEAM_APPROVAL_SECRET)")
    s.add_argument("id", type=int)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(f=cmd_approve)
    s = sub.add_parser("reject", help="reject a proposed email")
    s.add_argument("id", type=int)
    s.set_defaults(f=cmd_reject)
    sub.add_parser("decisions", help="the decisions waiting for a human").set_defaults(f=cmd_decisions)
    s = sub.add_parser("decide", help="approve or reject a pending decision, as a human")
    s.add_argument("id", type=int)
    s.add_argument("verdict", choices=["approve", "reject"])
    s.add_argument("--note", help="a change or a condition, passed to the owner")
    s.add_argument("--as", dest="as_")
    s.set_defaults(f=cmd_decide)
    sub.add_parser("promises", help="the team's open promises").set_defaults(f=cmd_promises)
    sub.add_parser("digest", help="print today's digest (doesn't post it)").set_defaults(f=cmd_digest)
    sub.add_parser("stop", help="stop starting new runs").set_defaults(f=cmd_stop)
    sub.add_parser("resume", help="resume").set_defaults(f=cmd_resume)
    s = sub.add_parser("mcp-config", help="print the MCP config for a human's own Claude Code session")
    s.add_argument("--as", dest="as_")
    s.set_defaults(f=cmd_mcp_config)
    s = sub.add_parser("prompt", help="print the system prompt an agent gets")
    s.add_argument("agent")
    s.set_defaults(f=cmd_prompt)
    sub.add_parser("discord-bot", help="run the optional Discord bot").set_defaults(f=cmd_discord)
    sub.add_parser("mailer", help="run the optional mailer").set_defaults(f=cmd_mailer)
    a = p.parse_args(argv)
    try:
        a.f(a)
    except config.ConfigError as e:
        raise SystemExit("team.json: %s" % e)


if __name__ == "__main__":
    main()
