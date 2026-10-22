"""Optional chat mirror (Discord). Off unless TEAM_DISCORD_ENABLED=1. When on, what agents say to the humans shows up
in a Discord channel, one thread per conversation, and the team's own initiatives share one thread a day.

Standard library only. Never raises: a chat outage must not break an agent's run."""
import json
import os
import time
import urllib.request

from . import config

API = "https://discord.com/api/v10"
UA = "agent-team/1.0"


def enabled():
    return config.flag("TEAM_DISCORD_ENABLED") and bool(config.setting("TEAM_DISCORD_WEBHOOK"))


def _post(url, payload):
    headers = {"content-type": "application/json", "user-agent": UA}
    if url.startswith(API + "/channels"):
        headers["authorization"] = "Bot " + config.setting("DISCORD_BOT_TOKEN")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    body = urllib.request.urlopen(req, timeout=10).read()
    return json.loads(body) if body else None


def _post_files(url, payload, paths):
    b = "agentteam%d" % int(time.time() * 1000)
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n%s\r\n"
              % (b, json.dumps(payload))).encode()]
    for i, p in enumerate(paths):
        name = os.path.basename(p).replace('"', "")
        with open(p, "rb") as f:
            data = f.read()
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files[%d]\"; filename=\"%s\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n" % (b, i, name)).encode() + data + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"content-type": "multipart/form-data; boundary=" + b, "user-agent": UA})
    body = urllib.request.urlopen(req, timeout=60).read()
    return json.loads(body) if body else None


def say(text, who=None, thread_id=None, files=None):
    """Posts to the channel (or one of its threads) as `who`. Long text goes in several messages."""
    if not enabled():
        return
    url = config.setting("TEAM_DISCORD_WEBHOOK") + (("?thread_id=%s" % thread_id) if thread_id else "")
    who = who or config.team().name
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)][:5] or [""]
    try:
        for i, chunk in enumerate(chunks):
            payload = {"username": who[:80], "content": chunk, "allowed_mentions": {"parse": []}}
            if files and i == len(chunks) - 1:
                _post_files(url, payload, files)
            else:
                _post(url, payload)
    except Exception as e:  # noqa: BLE001
        print("discord", repr(e)[:200], flush=True)


def chat_thread(c, tid, title, own=False):
    """The Discord thread for a conversation, created on its first message (needs the bot token). Conversations a
    human didn't start share one thread a day; approvals always get their own, so an "approve" can be traced to them."""
    r = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (tid,)).fetchone()
    if r:
        return r["external"]
    channel = config.setting("TEAM_DISCORD_CHANNEL")
    if not (channel and config.setting("DISCORD_BOT_TOKEN")):
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
        t = _post("%s/channels/%s/threads" % (API, channel), {"name": name, "type": 11, "auto_archive_duration": 4320})
    except Exception as e:  # noqa: BLE001
        print("discord thread", repr(e)[:200], flush=True)
        return None
    if daily:
        c.execute("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)", (key, t["id"]))
    else:
        c.execute("INSERT OR IGNORE INTO chat_threads (thread, external) VALUES (?, ?)", (tid, t["id"]))
    return t["id"]


def post(c, text, who, thread, title, own=False, files=None):
    if not enabled():
        return
    say(text, who=who, thread_id=chat_thread(c, thread, title, own=own), files=files)


def link_thread(c, tid, external_id):
    c.execute("INSERT OR REPLACE INTO chat_threads (thread, external) VALUES (?, ?)", (tid, str(external_id)))


def thread_of(c, external_id):
    r = c.execute("SELECT thread FROM chat_threads WHERE external = ?", (str(external_id),)).fetchone()
    return r["thread"] if r else None
