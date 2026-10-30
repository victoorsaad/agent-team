"""The office: a live 3D screen of the team (who's working, on what, who talks to whom). Serves office.html and two
JSON endpoints: /api/team (who is on the team) and /api/state (what they're doing, and what got done today).

Access, in order: localhost always; with TEAM_OFFICE_TOKEN set, a request carrying it (?token=… once, then a cookie);
behind an authenticating proxy (Cloudflare Access, oauth2-proxy…), a request from TEAM_OFFICE_TRUSTED_PROXIES whose
TEAM_OFFICE_EMAIL_HEADER is in TEAM_OFFICE_EMAILS. Binds to 127.0.0.1 by default.

Per team (team.json office): `title`, `back` ({"href", "label"}: a link to the page that embeds it), `prefix` (also
served under that path, e.g. "/office", for a proxy that routes it next to other pages) and `css` (the page's color
and font variables, e.g. {"bg": "#101820", "text": "#eef"}), so several teams on one host each get their own office.

`--replay file.json` plays a scripted, made-up conversation into a throwaway database, for demos."""
import hmac
import json
import os
import tempfile
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, store

HERE = os.path.dirname(os.path.abspath(__file__))


def page():
    """office.html with this team's title and colors inline (team.json office), so the first paint is already right."""
    o = config.team().office
    with open(os.path.join(HERE, "office.html"), encoding="utf-8") as f:
        html = f.read()
    css = "".join("--%s:%s;" % (k, v) for k, v in (o.get("css") or {}).items()
                  if str(k).replace("-", "").isalnum() and not any(ch in str(v) for ch in "<>{};"))
    if css:
        html = html.replace("</head>", "<style>:root{%s}</style>\n</head>" % css, 1)
    html = html.replace("<title>Agent Office</title>", "<title>%s</title>" % escape(o.get("title") or config.team().name), 1)
    return html.encode()


def snapshot(c=None):
    c = c or store.db()
    t = config.team()
    now = time.time()
    day = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    st = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM state")}
    max_run = config.number("TEAM_RUN_TIMEOUT_MIN", 45) * 60 + 300
    agents = []
    for a in t.roles:
        run = c.execute("SELECT * FROM runs WHERE agent = ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()
        working = bool(run and run["ended_at"] is None and now - run["started_at"] < max_run)
        acts = [dict(r) for r in c.execute("SELECT ts, text FROM activity WHERE agent = ? ORDER BY id DESC LIMIT 6", (a,))]
        waiting = c.execute("SELECT count(*) FROM messages WHERE recipient = ? AND status = 'new'", (a,)).fetchone()[0]
        tok, cost = c.execute("SELECT coalesce(sum(tokens_in + tokens_out), 0), coalesce(sum(cost_usd), 0) FROM runs "
                              "WHERE agent = ? AND started_at >= ?", (a, day)).fetchone()
        agents.append({
            "id": a, "name": t.display(a), "role": t.summary(a),
            "state": "working" if working else ("queued" if waiting else "idle"),
            "since": run["started_at"] if working else (run["ended_at"] if run else None),
            "subject": st.get("subject:" + a) if working else None,
            "actions": acts if working else acts[:1],
            "waiting": waiting,
            "runs_today": c.execute("SELECT count(*) FROM runs WHERE agent = ? AND started_at >= ?", (a, day)).fetchone()[0],
            "tokens_today": tok, "cost_today": cost,
            "last_ok": bool(run["ok"]) if run and run["ended_at"] else None,
        })
    msgs = [dict(r) for r in c.execute(
        "SELECT id, sender, recipient, kind, body, created_at FROM messages WHERE kind <> 'update' AND sender <> 'system' "
        "ORDER BY id DESC LIMIT 40")]
    feed = [{"t": m["created_at"], "from": m["sender"], "to": m["recipient"], "kind": m["kind"],
             "text": " ".join(m["body"].replace("**", "").split())[:260]} for m in msgs]
    convs = []
    for r in c.execute("SELECT thread, count(*) AS runs, sum(tokens_in + tokens_out) AS tokens, sum(cost_usd) AS cost, "
                       "max(ended_at) AS last, group_concat(DISTINCT agent) AS agents FROM runs WHERE started_at >= ? "
                       "AND thread IS NOT NULL GROUP BY thread ORDER BY cost DESC LIMIT 12", (day,)):
        root = c.execute("SELECT sender, body FROM messages WHERE id = ?", (r["thread"],)).fetchone()
        convs.append({"thread": r["thread"], "runs": r["runs"], "tokens": r["tokens"] or 0, "cost": r["cost"] or 0, "last": r["last"],
                      "agents": (r["agents"] or "").split(","), "from": root["sender"] if root else None,
                      "subject": " ".join((root["body"] if root else "").split())[:140]})
    done = [{"t": r["ts"], "agent": r["agent"], "origin": r["origin"], "text": r["text"], "proof": r["proof"]} for r in c.execute(
        "SELECT ts, agent, origin, text, proof FROM done WHERE ts >= ? UNION ALL "
        "SELECT created_at, sender, 'team', body, NULL FROM messages WHERE kind = 'share' AND created_at >= ? ORDER BY 1 DESC LIMIT 80",
        (day, day))]
    tot = c.execute("SELECT coalesce(sum(tokens_in + tokens_out), 0), coalesce(sum(cost_usd), 0) FROM runs WHERE started_at >= ?", (day,)).fetchone()
    return {
        "now": now, "conversations": convs, "done": done, "tokens_today": tot[0], "cost_today": tot[1], "agents": agents, "feed": feed,
        "paused_until": float(st.get("paused_until") or 0), "stopped": st.get("stopped") == "1", "replay": st.get("replay") == "1",
        "runs_today": c.execute("SELECT count(*) FROM runs WHERE started_at >= ?", (day,)).fetchone()[0],
        "msgs_today": c.execute("SELECT count(*) FROM messages WHERE created_at >= ? AND kind <> 'update'", (day,)).fetchone()[0],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-team-office"
    sys_version = ""

    def allowed(self):
        ip = self.client_address[0]
        token = config.setting("TEAM_OFFICE_TOKEN")
        if token:
            q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            cookie = dict(p.strip().split("=", 1) for p in (self.headers.get("Cookie") or "").split(";") if "=" in p).get("office_token", "")
            if hmac.compare_digest(q, token) or hmac.compare_digest(cookie, token):
                self._set_cookie = bool(q)
                return True
        if ip in ("127.0.0.1", "::1") and not config.flag("TEAM_OFFICE_REQUIRE_TOKEN"):
            return True
        proxies = {p.strip() for p in config.setting("TEAM_OFFICE_TRUSTED_PROXIES").split(",") if p.strip()}
        emails = {e.strip().lower() for e in config.setting("TEAM_OFFICE_EMAILS").split(",") if e.strip()}
        header = config.setting("TEAM_OFFICE_EMAIL_HEADER", "Cf-Access-Authenticated-User-Email")
        return ip in proxies and (self.headers.get(header) or "").lower() in emails

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        if getattr(self, "_set_cookie", False):
            self.send_header("set-cookie", "office_token=%s; HttpOnly; SameSite=Strict; Path=/" % config.setting("TEAM_OFFICE_TOKEN"))
        self.send_header("content-security-policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._set_cookie = False
        if not self.allowed():
            return self.reply(403, b"forbidden", "text/plain")
        path = urlparse(self.path).path
        prefix = "/" + (config.team().office.get("prefix") or "").strip("/")
        if prefix != "/" and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):] or "/"
            if path == "/" and not urlparse(self.path).path.endswith("/"):   # relative api/ links need the slash
                self.send_response(301)
                self.send_header("location", prefix + "/")
                self.end_headers()
                return
        if path == "/api/state":
            return self.reply(200, json.dumps(snapshot()).encode(), "application/json")
        if path == "/api/team":
            return self.reply(200, json.dumps(config.team().public()).encode(), "application/json")
        if path in ("/", "/index.html"):
            return self.reply(200, page(), "text/html; charset=utf-8")
        self.reply(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def serve(host=None, port=None, replay=None, speed=1.0):
    if replay:
        tmp = tempfile.mkdtemp(prefix="agent-team-replay-")
        os.environ["TEAM_DB"] = os.path.join(tmp, "replay.db")   # never touches the real mailbox
        from . import replay as rp
        threading.Thread(target=rp.play, args=(replay, os.environ["TEAM_DB"], speed), daemon=True).start()
    host = host or config.setting("TEAM_OFFICE_HOST", "127.0.0.1")
    port = int(port if port is not None else config.setting("TEAM_OFFICE_PORT", "8130"))
    server = ThreadingHTTPServer((host, port), Handler)
    print("office on http://%s:%d%s" % (host, server.server_address[1], " (replay)" if replay else ""), flush=True)
    return server
