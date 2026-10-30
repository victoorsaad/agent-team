"""Discord → team: the humans talk to the agents in one channel (rules in inbound.py). Needs team.json
`"chat": {"provider": "discord"}`, `pip install discord.py` and a bot with the Message Content intent and permission
to create threads.

Only the Discord accounts in TEAM_DISCORD_USERS (`<discord user id>:<human id>,…`) are heard. Attachments are saved
under TEAM_FILES/<message id>/ and their paths go in the message, so agents can open them. A webhook whose name is in
team.json alerts.names (an uptime or error monitor posting to the channel) opens an alert for the orchestrator."""
import os
import re

from .. import config, notify, store
from . import discord as provider
from . import inbound


def alert_text(msg):
    """The text of a monitor's post: its content plus the title and description of each embed."""
    parts = [msg.content or ""] + ["\n".join(x for x in (e.title, e.description) if x) for e in msg.embeds]
    return "\n\n".join(p for p in parts if p.strip()) or None


def build():
    import discord   # optional dependency

    t = config.team()
    channel_id = int(config.setting("TEAM_DISCORD_CHANNEL", "0"))
    users = {uid: who for who, uid in provider.user_ids().items()}
    named = inbound.namer()

    intents = discord.Intents.default()
    intents.message_content = True
    bot = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

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
        if msg.webhook_id and msg.channel.id == channel_id and inbound.is_alert_sender(msg.author.name):
            text = alert_text(msg)
            if text:
                c = store.db()
                mid = store.open_alert(c, text)
                th = await msg.create_thread(name=" ".join(text.split("\n")[0].split())[:90] or "Alert", auto_archive_duration=4320)
                notify.link_thread(c, mid, th.id)
                await msg.add_reaction("\N{EYES}")
            return
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
            replies, mid = inbound.handle(c, me, text[:9000], said, tid, msg.id, "discord")
            for r in replies:
                await msg.reply(r, mention_author=False)
            if mid:
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
    if config.team().chat_provider() != "discord":
        raise SystemExit("Discord is off: set team.json \"chat\": {\"provider\": \"discord\"}.")
    build().run(config.setting("DISCORD_BOT_TOKEN"), log_handler=None)
