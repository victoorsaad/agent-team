"""Optional: Discord → team. The humans talk to the agents in one channel, like a team chat. Off unless
TEAM_DISCORD_ENABLED=1; needs `pip install discord.py` and a bot with the Message Content intent and permission to
create threads.

- A new message in the channel starts a conversation in its own thread: "marketing, what's the angle for Friday?"
  goes to that agent; without a name it goes to the orchestrator.
- Inside a thread, just answer: it goes to the agent who spoke last there (or to the one you name).
- "approve" (or "approve 12") in a thread approves the emails pending in that conversation; the mailer re-checks it.
Only the Discord accounts in TEAM_DISCORD_USERS (`<discord user id>:<human id>,…`) are heard. Attachments are saved
under TEAM_FILES/<message id>/ and their paths go in the message, so agents can open them."""
import os
import re

from . import config, notify, store


def build():
    import discord   # optional dependency

    t = config.team()
    channel_id = int(config.setting("TEAM_DISCORD_CHANNEL", "0"))
    users = dict(p.strip().split(":", 1) for p in config.setting("TEAM_DISCORD_USERS").split(",") if ":" in p)
    names = {}
    for a, r in t.roles.items():
        names[a.lower()] = a
        names[r.get("name", a).lower()] = a
    name_re = re.compile(r"^\s*@?(%s)\b[:,]?\s*" % "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

    def named(text):
        m = name_re.match(text)
        return (names[m.group(1).lower()], text[m.end():]) if m else (None, text)

    def kind_of(body):
        return "ask" if body.rstrip().endswith("?") else "task"

    def approve_emails(c, text, tid, discord_id):
        if not store.APPROVE_RE.match(text):
            return
        ids = [int(n) for n in re.findall(r"\b(\d+)\b", text)]
        q = "UPDATE outbox SET status = 'approved', approved_by = 'discord', approval_ref = ? WHERE thread = ? AND status = 'pending'"
        args = [str(discord_id), tid]
        if ids:
            q += " AND id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        c.execute(q, args)

    def answer(c, me, text, tid):
        to, body = named(text)
        humans = list(t.humans)
        q = ",".join("?" * len(humans))
        last = c.execute("SELECT id, sender FROM messages WHERE thread = ? AND sender NOT IN (%s) AND sender <> 'system' " % q
                         + ("AND sender = ? " if to else "") + "ORDER BY id DESC LIMIT 1",
                         [tid] + humans + ([to] if to else [])).fetchone()
        if last:
            c.execute("UPDATE messages SET status = 'done' WHERE id = ? AND status <> 'done'", (last["id"],))
            return store.send(c, me, last["sender"], "reply", body, reply_to=last["id"], mirror=False)
        return store.send(c, me, to or t.orchestrator, kind_of(body), body, mirror=False, thread=tid)

    async def save_files(msg):
        lines = []
        for a in msg.attachments[:10]:
            name = re.sub(r"[^\w.\- ]", "_", a.filename).strip(". ") or "file"
            if a.size > 25 * 1024 * 1024:
                lines.append("- %s: too big, not saved; ask for a link" % name)
                continue
            folder = os.path.join(config.files_dir(), str(msg.id))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            await a.save(path)
            lines.append("- %s (%s, %d KB)" % (path, a.content_type or "file", max(1, a.size // 1024)))
        return "\n\n[Attached files, open with Read:]\n" + "\n".join(lines) if lines else ""

    @bot.event
    async def on_ready():
        print("discord bot up as", bot.user, "channel", channel_id, flush=True)

    @bot.event
    async def on_message(msg):
        in_thread = isinstance(msg.channel, discord.Thread) and msg.channel.parent_id == channel_id
        if msg.author.bot or msg.webhook_id or not (msg.channel.id == channel_id or in_thread):
            return
        me = users.get(str(msg.author.id))
        if me not in t.humans:
            print("ignored discord user", msg.author.id, flush=True)
            await msg.reply("I don't know you yet (id `%s`). Ask the team's owner to add you." % msg.author.id, mention_author=False)
            return
        text = re.sub(r"^(\s*<@[!&]?\d+>)+", "", msg.content or "").strip()
        if not text and not msg.attachments:
            return
        said, title = text, text or ", ".join(a.filename for a in msg.attachments)
        text = (text or "File attached.") + await save_files(msg)
        c = store.db()
        try:
            tid = notify.thread_of(c, msg.channel.id) if in_thread else None
            if tid:
                approve_emails(c, said, tid, msg.id)
                answer(c, me, text[:9000], tid)
            else:
                to, body = named(text[:9000])
                mid = store.send(c, me, to or t.orchestrator, kind_of(body), body, mirror=False)
                if in_thread:
                    notify.link_thread(c, mid, msg.channel.id)
                else:
                    th = await msg.create_thread(name=" ".join(named(title)[1].split())[:90] or "Conversation", auto_archive_duration=4320)
                    notify.link_thread(c, mid, th.id)
            await msg.add_reaction("\N{EYES}")
        except ValueError as e:
            await msg.reply(str(e), mention_author=False)

    return bot


def main():
    if not config.flag("TEAM_DISCORD_ENABLED"):
        raise SystemExit("Discord is off (TEAM_DISCORD_ENABLED=0).")
    build().run(config.setting("DISCORD_BOT_TOKEN"), log_handler=None)
