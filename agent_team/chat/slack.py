"""Slack: the bot posts as each agent (its name as the username, which needs the chat:write.customize scope), one
thread per conversation (the thread is a root message's ts), and reads messages back for the mailer. Standard library
only; the inbound bot (slack_bot.py) uses the Events API over HTTP (stdlib) or Socket Mode (`pip install slack_sdk`).

Settings: SLACK_BOT_TOKEN (xoxb-…), SLACK_CHANNEL (channel id, C…), TEAM_SLACK_USERS (`<slack user id>:<human id>,…`),
optional TEAM_SLACK_ICONS (`<agent id>::emoji:,…`). Bot scopes: chat:write, chat:write.customize, channels:history
(groups:history for a private channel), files:read, files:write, reactions:write, users:read."""
import json
import os
import re
import urllib.parse
import urllib.request

from .. import config, store
from . import parse_users

API = "https://slack.com/api/"
UA = "agent-team/0.3"
name = "slack"


class SlackError(RuntimeError):
    pass


def enabled():
    return bool(config.setting("SLACK_BOT_TOKEN") and config.setting("SLACK_CHANNEL"))


def call(method, payload=None, get=False, token=None):
    """One Web API call. Raises SlackError when Slack answers ok: false."""
    headers = {"authorization": "Bearer " + (token or config.setting("SLACK_BOT_TOKEN")), "user-agent": UA}
    if get:
        req = urllib.request.Request(API + method + "?" + urllib.parse.urlencode(payload or {}), headers=headers)
    else:
        headers["content-type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(API + method, data=json.dumps(payload or {}).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read() or b"{}")
    if not out.get("ok"):
        raise SlackError("%s: %s" % (method, out.get("error", "unknown error")))
    return out


def user_ids():
    return parse_users(config.setting("TEAM_SLACK_USERS"))


def icons():
    out = {}
    for pair in config.setting("TEAM_SLACK_ICONS").split(","):
        if "::" in pair:
            k, v = pair.split("::", 1)
            out[k.strip()] = ":" + v.strip().strip(":") + ":"
    return out


def mention(text, humans):
    ids = user_ids()
    users = [ids[h] for h in humans if h in ids]
    return (" ".join("<@%s>" % u for u in users) + " " + text) if users else text


def safe(text):
    """Slack's mrkdwn turns <!channel>, <!here> and <@U…> into pings: an agent's text never pings anyone by itself."""
    return text.replace("<!", "<​!").replace("<@", "<​@")


def upload(paths, thread_ts=None):
    channel = config.setting("SLACK_CHANNEL")
    done = []
    for p in paths:
        size = os.path.getsize(p)
        u = call("files.getUploadURLExternal", {"filename": os.path.basename(p), "length": size}, get=True)
        with open(p, "rb") as f:
            urllib.request.urlopen(urllib.request.Request(u["upload_url"], data=f.read(), method="POST",
                                                          headers={"user-agent": UA}), timeout=60).read()
        done.append({"id": u["file_id"], "title": os.path.basename(p)})
    if done:
        body = {"files": done, "channel_id": channel}
        if thread_ts:
            body["thread_ts"] = thread_ts
        call("files.completeUploadExternal", body)


def say(text, who=None, thread_id=None, files=None, ping=()):
    if not enabled():
        return
    t = config.team()
    who = who or t.name
    agent = next((k for k, r in t.roles.items() if r.get("name", k) == who), None)
    text = safe(text)
    if ping:
        text = mention(text, ping)
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)][:5] or [""]
    try:
        for chunk in chunks:
            body = {"channel": config.setting("SLACK_CHANNEL"), "text": chunk, "username": who[:80],
                    "unfurl_links": False, "unfurl_media": False}
            if agent and icons().get(agent):
                body["icon_emoji"] = icons()[agent]
            if thread_id:
                body["thread_ts"] = thread_id
            call("chat.postMessage", body)
        if files:
            upload(files, thread_id)
    except Exception as e:  # noqa: BLE001
        print("slack", repr(e)[:200], flush=True)


def new_thread(title):
    """A root message with the conversation's title; its ts is the thread."""
    if not enabled():
        return None
    out = call("chat.postMessage", {"channel": config.setting("SLACK_CHANNEL"), "text": "*%s*" % safe(title[:150]),
                                    "username": config.team().name[:80], "unfurl_links": False})
    return out["ts"]


def verify_approval(external, ref, code):
    """Reads the thread back from Slack: the message `ref` is from a listed human, says "approve", and an earlier
    message in the same thread showed the email with its code."""
    humans = set(user_ids().values())
    out = call("conversations.replies", {"channel": config.setting("SLACK_CHANNEL"), "ts": external, "limit": 200}, get=True)
    msgs = out.get("messages") or []
    msg = next((m for m in msgs if m.get("ts") == ref), None)
    if not msg or msg.get("user") not in humans or msg.get("bot_id") or not store.APPROVE_RE.match(clean(msg.get("text"))):
        return "the approval isn't a human's \"approve\""
    if not any("code %s" % code in (m.get("text") or "") for m in msgs if float(m.get("ts", 0)) < float(ref)):
        return "the email wasn't shown in the thread before the approval"
    return None


def clean(text):
    """A human's message without the leading @mentions of the bot ("<@U123> dev, …" → "dev, …")."""
    return re.sub(r"^(\s*<@[A-Z0-9]+>)+", "", text or "").strip()
