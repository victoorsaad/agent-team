"""The chat mirror: what agents say to the humans shows up in the team's chat (Discord or Slack, see chat/), one thread
per conversation, and the team's own initiatives share one thread a day. Off when team.json has no chat provider.

Never raises: a chat outage must not break an agent's run."""
import time

from . import chat, config

# What a human must answer or see gets an @mention; status updates and receipts don't.
PING_KINDS = ("ask", "task", "approval", "reply")


def enabled():
    return chat.enabled()


def pings(recipient, kind, force=False):
    """The humans a message @mentions: its recipient, when it's a human and the message asks something, asks for an
    approval or answers them. `force` (share with urgent) pings an update too. team.json chat.mention: false turns it off."""
    t = config.team()
    if not t.chat.get("mention", True):
        return []
    return [recipient] if t.is_human(recipient) and (kind in PING_KINDS or force) else []


def say(text, who=None, thread_id=None, files=None, ping=()):
    """Posts to the channel (or one of its threads) as `who`."""
    p = chat.provider()
    if p and p.enabled():
        p.say(text, who=who, thread_id=thread_id, files=files, ping=ping)


def chat_thread(c, tid, title, own=False):
    """The chat thread for a conversation, created on its first message. Conversations a human didn't start share one
    thread a day; approvals always get their own, so an "approve" can be traced to them."""
    r = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (tid,)).fetchone()
    if r:
        return r["external"]
    p = chat.provider()
    if not (p and p.enabled()):
        return None
    root = c.execute("SELECT sender FROM messages WHERE id = ?", (tid,)).fetchone()
    daily = not own and (not root or not config.team().is_human(root["sender"]))
    key = "daily:%s" % time.strftime("%Y-%m-%d")
    if daily:
        d = c.execute("SELECT v FROM state WHERE k = ?", (key,)).fetchone()
        if d:
            return d["v"]
        title = "%s · %s" % (config.team().name, time.strftime("%d/%m"))
    name = " ".join(title.split())[:90] or "Conversation"
    try:
        ext = p.new_thread(name)
    except Exception as e:  # noqa: BLE001
        print("chat thread", repr(e)[:200], flush=True)
        return None
    if not ext:
        return None
    if daily:
        c.execute("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)", (key, ext))
    else:
        c.execute("INSERT OR IGNORE INTO chat_threads (thread, external) VALUES (?, ?)", (tid, ext))
    return ext


def post(c, text, who, thread, title, own=False, files=None, ping=()):
    if not enabled():
        return
    say(text, who=who, thread_id=chat_thread(c, thread, title, own=own), files=files, ping=ping)


def link_thread(c, tid, external_id):
    c.execute("INSERT OR REPLACE INTO chat_threads (thread, external) VALUES (?, ?)", (tid, str(external_id)))


def thread_of(c, external_id):
    r = c.execute("SELECT thread FROM chat_threads WHERE external = ?", (str(external_id),)).fetchone()
    return r["thread"] if r else None


def external_of(c, tid):
    r = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (tid,)).fetchone()
    return r["external"] if r else None
