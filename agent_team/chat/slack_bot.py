"""Slack → team: the humans talk to the agents in one channel (rules in inbound.py). Needs team.json
`"chat": {"provider": "slack"}` and a Slack app with the bot scopes listed in slack.py, subscribed to the
message.channels event (message.groups for a private channel). Two ways to receive events:

- Socket Mode (no public URL; for a server behind a firewall): SLACK_APP_TOKEN (xapp-…, connections:write) and
  `pip install slack_sdk`.
- Events API over HTTP (standard library): SLACK_SIGNING_SECRET, and Slack's Request URL pointing at
  http(s)://<your host>/slack/events (TEAM_SLACK_HOST / TEAM_SLACK_PORT, default 127.0.0.1:8131, behind your proxy).
  Every request's signature is checked; a stale or unsigned one is refused.

Only the Slack users in TEAM_SLACK_USERS (`<slack user id>:<human id>,…`) are heard. A new message in the channel
starts a conversation whose thread is that message; files shared are saved under TEAM_FILES/<ts>/ for the agents. A bot
or integration whose name is in team.json alerts.names (a monitor posting to the channel) opens an alert."""
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import config, notify, store
from . import inbound
from . import slack as provider


def save_files(files, ts):
    lines = []
    for f in (files or [])[:10]:
        name = re.sub(r"[^\w.\- ]", "_", f.get("name") or "file").strip(". ") or "file"
        if (f.get("size") or 0) > 25 * 1024 * 1024 or not f.get("url_private_download"):
            lines.append("- %s: not saved (too big or not downloadable); ask for a link" % name)
            continue
        folder = os.path.join(config.files_dir(), re.sub(r"[^\d.]", "", ts))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        req = urllib.request.Request(f["url_private_download"],
                                     headers={"authorization": "Bearer " + config.setting("SLACK_BOT_TOKEN")})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as out:
            out.write(r.read(25 * 1024 * 1024))
        lines.append("- %s (%s, %d KB)" % (path, f.get("mimetype") or "file", max(1, (f.get("size") or 0) // 1024)))
    return "\n\n[Attached files, open with Read:]\n" + "\n".join(lines) if lines else ""


def reply(thread_ts, text):
    provider.call("chat.postMessage", {"channel": config.setting("SLACK_CHANNEL"), "thread_ts": thread_ts,
                                       "text": provider.safe(text), "username": config.team().name[:80]})


def react(ts):
    try:
        provider.call("reactions.add", {"channel": config.setting("SLACK_CHANNEL"), "timestamp": ts, "name": "eyes"})
    except Exception as e:  # noqa: BLE001
        print("slack reaction", repr(e)[:120], flush=True)


def process(ev, c=None):
    """One Slack event (the `event` object). Returns what it did, for the log and the tests."""
    if ev.get("type") != "message" or ev.get("channel") != config.setting("SLACK_CHANNEL"):
        return "ignored: not our channel"
    if ev.get("subtype") not in (None, "file_share", "bot_message", "thread_broadcast"):
        return "ignored: %s" % ev.get("subtype")   # edits, deletes, joins
    c = c or store.db()
    ts, parent = ev.get("ts"), ev.get("thread_ts")
    key = "slack:seen:%s" % ts
    if store.state(c, key):
        return "ignored: seen"   # Slack retries an event it thinks we missed
    store.state(c, key, 1)
    if ev.get("bot_id") or ev.get("subtype") == "bot_message":
        who = ev.get("username") or (ev.get("bot_profile") or {}).get("name")
        if not parent and inbound.is_alert_sender(who):
            text = "\n\n".join([ev.get("text") or ""] + ["\n".join(x for x in (a.get("title"), a.get("text")) if x)
                                                         for a in ev.get("attachments") or []]).strip()
            if text:
                notify.link_thread(c, store.open_alert(c, text), ts)
                react(ts)
                return "alert"
        return "ignored: bot"
    users = {uid: who for who, uid in provider.user_ids().items()}
    me = users.get(ev.get("user"))
    if me not in config.team().humans:
        print("ignored slack user", ev.get("user"), flush=True)
        reply(parent or ts, "I don't know you yet (id `%s`). Ask the team's owner to add you." % ev.get("user"))
        return "ignored: unknown user"
    said = provider.clean(ev.get("text"))
    if not said and not ev.get("files"):
        return "ignored: empty"
    text = (said or "File attached.") + save_files(ev.get("files"), ts)
    tid = notify.thread_of(c, parent) if parent else None
    try:
        replies, mid = inbound.handle(c, me, text[:9000], said, tid, ts, "slack")
    except ValueError as e:
        reply(parent or ts, str(e))
        return "refused"
    for r in replies:
        reply(parent or ts, r)
    if mid:
        notify.link_thread(c, mid, parent or ts)   # a top-level message is its own thread
    react(ts)
    return "conversation %s" % (mid or tid)


# ---- Events API over HTTP (stdlib) ---------------------------------------------------------------------------------

def signed(secret, ts, body, sig, now=None):
    """Slack's request signature (v0): HMAC-SHA256 of "v0:<ts>:<body>", and the request no older than 5 minutes."""
    try:
        if abs((now or time.time()) - int(ts)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    mine = "v0=" + hmac.new(secret.encode(), b"v0:" + str(ts).encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig or "")


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-team-slack"
    sys_version = ""

    def do_POST(self):
        body = self.rfile.read(min(int(self.headers.get("content-length") or 0), 1_000_000))
        if self.path.split("?")[0] != "/slack/events" or not signed(config.setting("SLACK_SIGNING_SECRET"),
                                                                     self.headers.get("X-Slack-Request-Timestamp"), body,
                                                                     self.headers.get("X-Slack-Signature")):
            return self.answer(403, b"forbidden")
        try:
            data = json.loads(body)
        except ValueError:
            return self.answer(400, b"bad json")
        if data.get("type") == "url_verification":
            return self.answer(200, json.dumps({"challenge": data.get("challenge", "")}).encode(), "application/json")
        self.answer(200, b"")   # Slack wants an answer within 3 s; the work happens after
        if data.get("type") == "event_callback":
            threading.Thread(target=self.work, args=(data.get("event") or {},), daemon=True).start()

    @staticmethod
    def work(ev):
        try:
            print("slack", process(ev), flush=True)
        except Exception as e:  # noqa: BLE001
            print("slack event", repr(e)[:300], flush=True)

    def answer(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve_http():
    if not config.setting("SLACK_SIGNING_SECRET"):
        raise SystemExit("SLACK_SIGNING_SECRET is not set (or use Socket Mode with SLACK_APP_TOKEN).")
    host, port = config.setting("TEAM_SLACK_HOST", "127.0.0.1"), config.number("TEAM_SLACK_PORT", 8131)
    server = ThreadingHTTPServer((host, port), Handler)
    print("slack events on http://%s:%d/slack/events" % (host, port), flush=True)
    server.serve_forever()


def serve_socket():
    from slack_sdk.socket_mode import SocketModeClient   # optional dependency
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient

    client = SocketModeClient(app_token=config.setting("SLACK_APP_TOKEN"), web_client=WebClient(token=config.setting("SLACK_BOT_TOKEN")))

    def on_request(cl, req):
        cl.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "events_api":
            Handler.work((req.payload or {}).get("event") or {})

    client.socket_mode_request_listeners.append(on_request)
    client.connect()
    print("slack bot up (socket mode)", flush=True)
    threading.Event().wait()


def main():
    if config.team().chat_provider() != "slack":
        raise SystemExit("Slack is off: set team.json \"chat\": {\"provider\": \"slack\"}.")
    if not provider.enabled():
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_CHANNEL are required.")
    (serve_socket if config.setting("SLACK_APP_TOKEN") else serve_http)()
