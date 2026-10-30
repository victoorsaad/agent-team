"""Discord: a webhook posts as each agent (its name as the username), the bot token creates threads and reads messages
back for the mailer. Standard library only; the inbound bot (discord_bot.py) needs `pip install discord.py`.

Settings: DISCORD_BOT_TOKEN, TEAM_DISCORD_CHANNEL (channel id), TEAM_DISCORD_WEBHOOK (a webhook of that channel),
TEAM_DISCORD_USERS (`<discord user id>:<human id>,…`)."""
import json
import os
import time
import urllib.request

from .. import config, store
from . import parse_users

API = "https://discord.com/api/v10"
UA = "agent-team/0.3"
name = "discord"


def enabled():
    return bool(config.setting("TEAM_DISCORD_WEBHOOK"))


def _request(url, payload=None, method="POST"):
    headers = {"content-type": "application/json", "user-agent": UA}
    if url.startswith(API):
        headers["authorization"] = "Bot " + config.setting("DISCORD_BOT_TOKEN")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method if data is not None else "GET", headers=headers)
    body = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(body) if body else None


def _post_files(url, payload, paths):
    b = "agentteam%d" % int(time.time() * 1000)
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n%s\r\n"
              % (b, json.dumps(payload))).encode()]
    for i, p in enumerate(paths):
        fname = os.path.basename(p).replace('"', "")
        with open(p, "rb") as f:
            data = f.read()
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files[%d]\"; filename=\"%s\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n" % (b, i, fname)).encode() + data + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"content-type": "multipart/form-data; boundary=" + b, "user-agent": UA})
    body = urllib.request.urlopen(req, timeout=60).read()
    return json.loads(body) if body else None


def user_ids():
    return {k: v for k, v in parse_users(config.setting("TEAM_DISCORD_USERS")).items() if v.isdigit()}


def mention(text, humans):
    """`text` starting with the humans' <@id> and the allowed_mentions that ping exactly them (no @everyone, no roles,
    nobody quoted in the text)."""
    ids = user_ids()
    users = [ids[h] for h in humans if h in ids]
    if not users:
        return text, {"parse": []}
    return " ".join("<@%s>" % u for u in users) + " " + text, {"parse": [], "users": users}


def say(text, who=None, thread_id=None, files=None, ping=()):
    if not enabled():
        return
    url = config.setting("TEAM_DISCORD_WEBHOOK") + (("?thread_id=%s" % thread_id) if thread_id else "")
    who = who or config.team().name
    text, allowed = mention(text, ping) if ping else (text, {"parse": []})
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)][:5] or [""]
    try:
        for i, chunk in enumerate(chunks):
            payload = {"username": who[:80], "content": chunk, "allowed_mentions": allowed if i == 0 else {"parse": []}}
            if files and i == len(chunks) - 1:
                _post_files(url, payload, files)
            else:
                _request(url, payload)
    except Exception as e:  # noqa: BLE001
        print("discord", repr(e)[:200], flush=True)


def new_thread(title):
    channel = config.setting("TEAM_DISCORD_CHANNEL")
    if not (channel and config.setting("DISCORD_BOT_TOKEN")):
        return None
    t = _request("%s/channels/%s/threads" % (API, channel), {"name": title[:90], "type": 11, "auto_archive_duration": 4320})
    return t["id"]


def verify_approval(external, ref, code):
    humans = set(user_ids().values())
    msg = _request("%s/channels/%s/messages/%s" % (API, external, ref))
    if (msg.get("author") or {}).get("id") not in humans or not store.APPROVE_RE.match(msg.get("content") or ""):
        return "the approval isn't a human's \"approve\""
    before = _request("%s/channels/%s/messages?before=%s&limit=50" % (API, external, ref)) or []
    if not any("code %s" % code in (m.get("content") or "") for m in before):
        return "the email wasn't shown in the thread before the approval"
    return None
