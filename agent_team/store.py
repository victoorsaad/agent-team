"""The team's mailbox: who asked whom, what, and the answer. One SQLite file shared by the MCP server (every agent and
every human session talks through it), the dispatcher (wakes an agent when it has mail) and the office (reads it).

The mailbox is a coordination tool, not a security boundary: any process that can open the file can write to it.
Side effects that matter (an email leaving, for instance) are verified out of band; see mailer.py and SECURITY.md."""
import hashlib
import hmac
import os
import re
import sqlite3
import time

from . import config, notify

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread INTEGER, sender TEXT NOT NULL, recipient TEXT NOT NULL,
  kind TEXT NOT NULL,                    -- ask | task | reply | update | approval
  body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',   -- new | read | done
  reply_to INTEGER, created_at REAL NOT NULL, read_at REAL);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages (recipient, status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, agent TEXT, started_at REAL, ended_at REAL, ok INTEGER, note TEXT,
  thread INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, turns INTEGER);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, run_id INTEGER, agent TEXT, ts REAL, text TEXT);
CREATE INDEX IF NOT EXISTS activity_agent ON activity (agent, ts);
CREATE TABLE IF NOT EXISTS outbox (           -- emails an agent proposed; mailer.py sends them once a human approves
  id INTEGER PRIMARY KEY, agent TEXT NOT NULL, thread INTEGER, to_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  campaign TEXT, code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | sent | failed | rejected
  approved_by TEXT, approval_ref TEXT, error TEXT, created_at REAL NOT NULL, sent_at REAL);
CREATE TABLE IF NOT EXISTS chat_threads (thread INTEGER PRIMARY KEY, external TEXT UNIQUE NOT NULL);  -- conversation → chat thread
"""

# The whole message must be an approval, so "ok, but change X" is not one. English and Portuguese out of the box.
APPROVE_RE = re.compile(r"^\s*(approve[d]?|aprovo|aprovad[oa]s?|lgtm|go|ok|yes|sim|send it|pode enviar|manda|envia)"
                        r"(\s+(\d+([\s,]+(and\s+|e\s+)?\d+)*|all|todos|tudo))?\s*[.!]*\s*$", re.I)

MAX_FILE = 10 * 1024 * 1024
MAX_FILES = 10
# Paths agents may attach: never dotfiles/dot folders (.git, .ssh, .claude, .env), key files or credentials.
SECRET_RE = re.compile(r"(^|/)(\.[^/]*|env|[^/]*\.(key|pem|p12|pfx|der|env)|id_[a-z0-9]+|credentials[^/]*)(/|$)", re.I)


def db(path=None):
    path = path or config.db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def state(c, k, v=None):
    if v is None:
        r = c.execute("SELECT v FROM state WHERE k = ?", (k,)).fetchone()
        return r["v"] if r else None
    c.execute("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)", (k, str(v)))


def email_code(to, subject, body):
    """Short fingerprint of an email, shown with it to the approver; the mailer sends only if the row still matches."""
    return hashlib.sha256(("%s\n%s\n%s" % (to.strip().lower(), subject.strip(), body.strip())).encode()).hexdigest()[:8]


def approval_sig(secret, outbox_id, code):
    """What `agent-team approve` writes: only someone holding TEAM_APPROVAL_SECRET can produce it."""
    return hmac.new(secret.encode(), ("%s:%s" % (outbox_id, code)).encode(), hashlib.sha256).hexdigest()


def out_roots():
    extra = [p for p in config.setting("TEAM_FILE_ROOTS", "").split(":") if p]
    return tuple(os.path.realpath(p).rstrip("/") + "/" for p in [config.workdir(), config.files_dir(), "/tmp"] + extra)


def outgoing(paths):
    """The files an agent wants to send, checked: real files under the work dir, the team's files or /tmp, never
    secrets (env files, keys, anything under a dot folder), at most 10 of 10 MB. Raises with the reason."""
    out = []
    roots = out_roots()
    for p in (paths or [])[:MAX_FILES + 1]:
        real = os.path.realpath(os.path.expanduser(str(p)))
        if len(out) >= MAX_FILES:
            raise ValueError("At most %d files per message." % MAX_FILES)
        if not real.startswith(roots) or SECRET_RE.search(real):
            raise ValueError("Can't send %s: only files under %s, and never keys or env files." % (p, ", ".join(roots)))
        if not os.path.isfile(real):
            raise ValueError("No file at %s." % p)
        if os.path.getsize(real) > MAX_FILE:
            raise ValueError("%s is over 10 MB: compress it or send a link." % p)
        out.append(real)
    return out


def attached(paths):
    return "\n\n[Files: %s]" % ", ".join(paths) if paths else ""


def shown(sender, recipient):
    """Only what's said to a human reaches the chat mirror. Agents talking to each other stay in the mailbox."""
    t = config.team()
    return sender != "system" and not t.is_human(sender) and t.is_human(recipient)


def send(c, sender, recipient, kind, body, reply_to=None, mirror=True, thread=None, files=None):
    """One message. `thread` (or `reply_to`) keeps it in an existing conversation; otherwise it starts one."""
    t = config.team()
    if recipient not in t.members:
        raise ValueError("Unknown teammate %r. The team: %s" % (recipient, ", ".join(t.members)))
    if reply_to:
        r = c.execute("SELECT id, thread FROM messages WHERE id = ?", (reply_to,)).fetchone()
        if not r:
            raise ValueError("No message %s" % reply_to)
        thread = r["thread"] or r["id"]
    mid = c.execute("INSERT INTO messages (thread, sender, recipient, kind, body, reply_to, created_at) VALUES (?,?,?,?,?,?,?)",
                    (thread, sender, recipient, kind, body + attached(files), reply_to, time.time())).lastrowid
    if thread is None:
        thread = mid
        c.execute("UPDATE messages SET thread = ? WHERE id = ?", (mid, mid))
    if mirror and shown(sender, recipient):
        title = body if thread == mid else c.execute("SELECT body FROM messages WHERE id = ?", (thread,)).fetchone()["body"]
        notify.post(c, body, who=t.display(sender), thread=thread, title=title, own=kind == "approval", files=files)
    return mid


def inbox(c, who, mark=True):
    """The unread messages of ONE conversation (the oldest waiting), so a run works on one subject and everything it
    sends stays in that conversation. Humans share one inbox (any of them can answer)."""
    t = config.team()
    ids = list(t.humans) if t.is_human(who) else [who]
    q = ",".join("?" * len(ids))
    first = c.execute("SELECT coalesce(thread, id) AS t FROM messages WHERE recipient IN (%s) AND status = 'new' ORDER BY id LIMIT 1" % q,
                      ids).fetchone()
    if not first:
        return []
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM messages WHERE recipient IN (%s) AND status = 'new' AND coalesce(thread, id) = ? ORDER BY id" % q, ids + [first["t"]])]
    if mark:
        c.executemany("UPDATE messages SET status = 'read', read_at = ? WHERE id = ?", [(time.time(), r["id"]) for r in rows])
    return rows


def thread(c, tid):
    return [dict(r) for r in c.execute("SELECT * FROM messages WHERE thread = ? OR id = ? ORDER BY id", (tid, tid))]


def log(c, limit=30, agent=None):
    q = "SELECT * FROM messages" + (" WHERE sender = ? OR recipient = ?" if agent else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in c.execute(q, ((agent, agent, limit) if agent else (limit,)))][::-1]


def fmt(rows):
    if not rows:
        return "Nothing."
    return "\n\n".join("#%d [%s] %s → %s%s · %s\n%s" % (
        r["id"], r["kind"], r["sender"], r["recipient"], " (re #%d)" % r["reply_to"] if r["reply_to"] else "",
        time.strftime("%d/%m %H:%M", time.localtime(r["created_at"])), r["body"]) for r in rows)
