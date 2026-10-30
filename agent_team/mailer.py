"""Optional: sends the emails agents proposed (`propose_email`) once a human approved them. Off unless
TEAM_MAILER_ENABLED=1.

Agents can write to the mailbox, so a row marked "approved" proves nothing by itself. Before each send the mailer
checks, out of band, that a human approved this exact email (same recipient, subject and body as when it was shown):
- approved with `agent-team approve <id>`: an HMAC of (id, code) made with TEAM_APPROVAL_SECRET, which agents must
  not be able to read (run agents as another OS user; see SECURITY.md);
- approved in the chat thread (Discord or Slack): the mailer reads that message from the chat itself and checks its
  author is a listed human (TEAM_DISCORD_USERS / TEAM_SLACK_USERS), that it says "approve" and that the email (with its
  code) was shown in that thread before it.
Then it sends through SMTP, at most MAILER_DAILY a day, optionally only inside MAILER_WINDOW hours (MAILER_TZ).

With MAILER_CHECK_HOSTS (your own site, e.g. shop.example.com), every link in the body to one of those hosts must
exist: a 404 or 410 rejects the email (an agent stitched a link together), a 5xx or a network error leaves it approved
for the next round (the site is restarting). A reply ("Re: <subject>") may only carry links the first email to that
person already had."""
import hmac
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from . import chat, config, notify, store

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
    p = chat.provider()
    if p and row["approved_by"] == p.name:
        d = c.execute("SELECT external FROM chat_threads WHERE thread = ?", (row["thread"],)).fetchone()
        if not d or not ref:
            return "no chat thread or approval message"
        return p.verify_approval(d["external"], ref, row["code"])
    return "no verifiable approval"


LINK_RE = re.compile(r"https?://([^/\s\"'<>]+)(/[^\s\"'<>]*)?", re.I)


def links(body):
    """The links in a body that point to MAILER_CHECK_HOSTS (trailing punctuation trimmed)."""
    hosts = {h.strip().lower() for h in config.setting("MAILER_CHECK_HOSTS").split(",") if h.strip()}
    out = set()
    for m in LINK_RE.finditer(body or ""):
        if m.group(1).lower().split(":")[0] in hosts:
            out.add(m.group(0).rstrip(".,;:!?)]"))
    return out


def link_exists(url):
    """True / False (404 or 410). Anything else that fails (5xx, network) raises, so the email waits for the next round."""
    req = urllib.request.Request(url, headers={"User-Agent": "agent-team-mailer/1.0"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        raise RuntimeError("link check %s: %s" % (url, e.code)) from None


def links_ok(c, row):
    """None when the email's links to our own hosts are real (and, for a "Re:", the first email's), else why not."""
    found = links(row["body"])
    for url in sorted(found):
        if not link_exists(url):
            return "the link %s doesn't exist" % url
    if found and row["subject"].strip().lower().startswith("re:"):
        first = re.sub(r"^(re:\s*)+", "", row["subject"].strip(), flags=re.I)
        earlier = c.execute("SELECT body FROM outbox WHERE lower(to_email) = lower(?) AND status = 'sent' AND subject = ?",
                            (row["to_email"], first)).fetchall()
        before = set().union(*[links(e["body"]) for e in earlier]) if earlier else set()
        if before and not found <= before:
            return "the reply links somewhere the first email didn't"
    return None


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
            why = verify(c, row) or links_ok(c, row)
            if why:
                c.execute("UPDATE outbox SET status = 'rejected', error = ? WHERE id = ?", (why, row["id"]))
                notify.say("Email %d not sent: %s." % (row["id"], why), thread_id=where)
                continue
            send(row)
            c.execute("UPDATE outbox SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?", (time.time(), row["id"]))
            notify.say("Sent to %s: %s" % (row["to_email"], row["subject"]), thread_id=where)
            store.did(c, row["agent"], "Email to %s: %s" % (row["to_email"], row["subject"]), thread=row["thread"])
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
