"""Optional: sends the emails agents proposed (`propose_email`) once a human approved them. Off unless
TEAM_MAILER_ENABLED=1.

Agents can write to the mailbox, so a row marked "approved" proves nothing by itself. Before each send the mailer
checks, out of band, that a human approved this exact email (same recipient, subject and body as when it was shown):
- approved with `agent-team approve <id>`: an HMAC of (id, code) made with TEAM_APPROVAL_SECRET, which agents must
  not be able to read (run agents as another OS user; see SECURITY.md);
- approved in the Discord thread: the mailer reads that message from Discord itself and checks its author is a human
  in TEAM_DISCORD_USERS, that it says "approve" and that the email (with its code) was shown before it.
Then it sends through SMTP, at most MAILER_DAILY a day, optionally only inside MAILER_WINDOW hours (MAILER_TZ)."""
import hmac
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from . import config, notify, store

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def in_window(now=None):
    win = config.setting("MAILER_WINDOW")   # e.g. "9-12"; empty = any time
    if not win:
        return True
    tz = config.setting("MAILER_TZ", "UTC")
    now = now or datetime.now(ZoneInfo(tz) if ZoneInfo else None)
    lo, hi = (int(h) for h in win.split("-"))
    weekdays_only = config.flag("MAILER_WEEKDAYS_ONLY", True)
    return (now.weekday() < 5 or not weekdays_only) and lo <= now.hour < hi


def discord_get(path):
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + config.setting("DISCORD_BOT_TOKEN"), "User-Agent": "agent-team-mailer/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def verify(c, row):
    """None when the approval holds, else why not."""
    if row["code"] != store.email_code(row["to_email"], row["subject"], row["body"]):
        return "the email changed after it was shown"
    ref = row["approval_ref"] or ""
    if row["approved_by"] == "cli":
        secret = config.setting("TEAM_APPROVAL_SECRET")
        if not secret:
            return "TEAM_APPROVAL_SECRET is not set for the mailer"
        if not hmac.compare_digest(ref, store.approval_sig(secret, row["id"], row["code"])):
            return "the approval signature doesn't match"
        return None
    if row["approved_by"] == "discord" and config.flag("TEAM_DISCORD_ENABLED"):
        humans = {p.split(":", 1)[0].strip() for p in config.setting("TEAM_DISCORD_USERS").split(",") if ":" in p}
        d = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (row["thread"],)).fetchone()
        if not d or not ref:
            return "no chat thread or approval message"
        msg = discord_get("/channels/%s/messages/%s" % (d["external"], ref))
        if msg.get("author", {}).get("id") not in humans or not store.APPROVE_RE.match(msg.get("content") or ""):
            return "the approval isn't a human's \"approve\""
        before = discord_get("/channels/%s/messages?before=%s&limit=50" % (d["external"], ref))
        if not any("code %s" % row["code"] in (m.get("content") or "") for m in before):
            return "the email wasn't shown in the thread before the approval"
        return None
    return "no verifiable approval"


def send(row):
    if config.flag("TEAM_DRY_RUN"):
        print("mailer (dry run) would send to %s: %s" % (row["to_email"], row["subject"]), flush=True)
        return "dry-run"
    msg = EmailMessage()
    msg["From"] = config.setting("MAILER_FROM")
    msg["To"] = row["to_email"]
    msg["Subject"] = row["subject"]
    msg.set_content(row["body"])
    host, port = config.setting("SMTP_HOST"), config.number("SMTP_PORT", 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        if config.flag("SMTP_STARTTLS", True):
            s.starttls(context=ssl.create_default_context())
        if config.setting("SMTP_USER"):
            s.login(config.setting("SMTP_USER"), config.setting("SMTP_PASSWORD"))
        s.send_message(msg)
    return msg.get("Message-ID") or "sent"


def tick(c):
    if not in_window():
        return
    daily = config.number("MAILER_DAILY", 25)
    for row in c.execute("SELECT * FROM outbox WHERE status = 'approved' ORDER BY id").fetchall():
        d = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (row["thread"],)).fetchone()
        where = d["external"] if d else None
        if c.execute("SELECT count(*) FROM outbox WHERE status = 'sent' AND sent_at >= ?", (time.time() - 86400,)).fetchone()[0] >= daily:
            print("daily cap reached", flush=True)
            return
        try:
            why = verify(c, row)
            if why:
                c.execute("UPDATE outbox SET status = 'rejected', error = ? WHERE id = ?", (why, row["id"]))
                notify.say("Email %d not sent: %s." % (row["id"], why), thread_id=where)
                continue
            send(row)
            c.execute("UPDATE outbox SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?", (time.time(), row["id"]))
            notify.say("Sent to %s: %s" % (row["to_email"], row["subject"]), thread_id=where)
        except (smtplib.SMTPException, urllib.error.HTTPError) as e:
            c.execute("UPDATE outbox SET status = 'failed', error = ? WHERE id = ?", (repr(e)[:300], row["id"]))
            notify.say("Email %d failed: %s" % (row["id"], repr(e)[:150]), thread_id=where)
        except Exception as e:  # noqa: BLE001  network hiccup: stays approved, tried again next round
            print("mailer", row["id"], repr(e)[:200], flush=True)


def main():
    if not config.flag("TEAM_MAILER_ENABLED"):
        raise SystemExit("The mailer is off (TEAM_MAILER_ENABLED=0).")
    c = store.db()
    print("mailer up; from", config.setting("MAILER_FROM"), flush=True)
    while True:
        try:
            tick(c)
        except Exception as e:  # noqa: BLE001
            print("tick", repr(e)[:300], flush=True)
        time.sleep(10)
