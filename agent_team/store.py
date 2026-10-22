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
  kind TEXT NOT NULL,                    -- ask | task | reply | update | approval | share (for the digest) | digest
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
CREATE TABLE IF NOT EXISTS decisions (        -- what waits for a human (request_approval, the merge gate); emails stay in outbox
  id INTEGER PRIMARY KEY, thread INTEGER, kind TEXT NOT NULL, ref TEXT,   -- ref: pr:<number> for a pull request
  what TEXT NOT NULL, owner TEXT NOT NULL, message_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
  decided_by TEXT, note TEXT, created_at REAL NOT NULL, decided_at REAL);
CREATE INDEX IF NOT EXISTS decisions_open ON decisions (status, thread);
CREATE TABLE IF NOT EXISTS promises (         -- "I'll do X later" (`commit`): the dispatcher wakes the agent for it
  id INTEGER PRIMARY KEY, agent TEXT NOT NULL, thread INTEGER, what TEXT NOT NULL,
  due REAL NOT NULL, depends TEXT,            -- pr:<number> (merged) | decision:<id> (decided)
  status TEXT NOT NULL DEFAULT 'open',        -- open | done | failed
  proof TEXT, created_at REAL NOT NULL, closed_at REAL, woke_at REAL, chased_at REAL);
CREATE INDEX IF NOT EXISTS promises_open ON promises (status, due);
CREATE TABLE IF NOT EXISTS done (             -- the ledger of what got done: the office's "Done today" and the digest
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, agent TEXT NOT NULL, origin TEXT NOT NULL,   -- human | routine | team
  text TEXT NOT NULL, proof TEXT, thread INTEGER);
CREATE INDEX IF NOT EXISTS done_ts ON done (ts);
"""

# The whole message must be an approval, so "ok, but change X" is not one. English and Portuguese out of the box.
APPROVE_RE = re.compile(r"^\s*(approve[d]?|aprovo|aprovad[oa]s?|lgtm|go|ok|yes|sim|send it|pode enviar|manda|envia)"
                        r"(\s+(the\s+|o\s+|os\s+)?e-?mails?)?"
                        r"(\s+(\d+([\s,]+(and\s+|e\s+)?\d+)*|all|todos|tudo))?\s*[.!]*\s*$", re.I)
# An approval that says it's about the emails ("approve the emails", "send it"). Bare numbers ("approve 12 13") count
# only when they are pending email ids (email_approval): "approve 48" with PR #48 pending is about the PR.
EMAILS_RE = re.compile(r"\be-?mails?\b|\bsend\b|\b(envi|mand)", re.I)
# "I'll let you know when…" with nothing registered is how requests got lost: such a message needs a `commit` first.
PROMISE_RE = re.compile(r"\b(i'?ll|i will|we'?ll) (let you know|get back|report back|send|do|check|ping)\b[^.?!]{0,40}\b(when|once|after|later|tomorrow|tonight)\b|"
                        r"\bonce the (deploy|merge|ci|pr)\b|\bafter the (deploy|merge)\b|\bnext run\b|"
                        r"\bte aviso\b|\baviso (quando|assim)\b|\b(mando|faço|confiro|rodo|respondo|volto)( \S+){0,4} (quando|assim que|depois)\b|"
                        r"\bdepois do (deploy|merge)\b|\bna próxima rodada\b", re.I)

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
    if "origin" not in {r[1] for r in c.execute("PRAGMA table_info(runs)")}:   # mailboxes from v0.1
        c.execute("ALTER TABLE runs ADD COLUMN origin TEXT")
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


def origin_of(c, tid):
    """Who started a conversation: a human, a routine (system) or the team."""
    r = tid and c.execute("SELECT sender FROM messages WHERE id = ?", (tid,)).fetchone()
    return "human" if r and config.team().is_human(r["sender"]) else "routine" if r and r["sender"] == "system" else "team"


def did(c, agent, text, proof=None, thread=None):
    """One line in the ledger of what got done (office → Done today, the digest). Never fails the caller."""
    try:
        c.execute("INSERT INTO done (ts, agent, origin, text, proof, thread) VALUES (?, ?, ?, ?, ?, ?)",
                  (time.time(), agent, origin_of(c, thread), " ".join(text.split())[:300], proof, thread))
    except sqlite3.Error as e:
        print("done", repr(e)[:200], flush=True)


# ---- decisions: what waits for a human ------------------------------------------------------------------------------

def pending(c, tid):
    """What waits for a human in one conversation: open decisions and pending emails, oldest first."""
    dec = [dict(r) for r in c.execute("SELECT * FROM decisions WHERE thread = ? AND status = 'pending' ORDER BY id", (tid,))]
    mails = [dict(r) for r in c.execute("SELECT id, to_email, subject FROM outbox WHERE thread = ? AND status = 'pending' ORDER BY id", (tid,))]
    return dec, mails


def pending_text(c, tid):
    """The same, as lines for an agent's prompt (the orchestrator resolves a human's "approve" against this)."""
    dec, mails = pending(c, tid)
    lines = ["- decision %d (%s%s, owner %s): %s" % (d["id"], d["kind"], " " + d["ref"] if d["ref"] else "", d["owner"],
                                                    " ".join(d["what"].split())[:200]) for d in dec]
    if mails:
        lines.append("- %d email(s) pending: no. %s" % (len(mails), ", ".join(str(m["id"]) for m in mails)))
    return "\n".join(lines)


def open_decision(c, sender, kind, what, thread=None, ref=None, owner=None, files=None, mirror=True):
    """A pending decision plus the message that shows it to the approvers, in `thread`. Returns (id, message id); the
    message id is None when the same ref already waits (one question per PR)."""
    if ref:
        old = c.execute("SELECT id FROM decisions WHERE ref = ? AND status = 'pending'", (ref,)).fetchone()
        if old:
            return old["id"], None
    did_ = c.execute("INSERT INTO decisions (thread, kind, ref, what, owner, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (thread, kind, ref, what, owner or sender, time.time())).lastrowid
    mid = send(c, sender, config.team().approvers()[0], "approval", what, thread=thread, files=files, mirror=mirror)
    c.execute("UPDATE decisions SET message_id = ?, thread = coalesce(thread, ?) WHERE id = ?", (mid, mid, did_))
    return did_, mid


def decide(c, decision_id, verdict, by, note=None, sender=None):
    """Record a human's answer to a pending decision and tell its owner. `sender` relays it (the orchestrator, or the
    human themself). Who may call this is checked by the caller (mcp_server, the CLI)."""
    d = c.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if not d or d["status"] != "pending":
        raise ValueError("Decision %s isn't pending." % decision_id)
    if verdict not in ("approved", "rejected"):
        raise ValueError("verdict is approved or rejected")
    t = config.team()
    c.execute("UPDATE decisions SET status = ?, decided_by = ?, note = ?, decided_at = ? WHERE id = ?",
              (verdict, by, note, time.time(), d["id"]))
    did(c, by, "%s %s: %s" % (t.display(by), verdict, d["what"]), thread=d["thread"])
    if d["owner"] != (sender or by) and d["owner"] in t.roles:
        send(c, sender or by, d["owner"], "task", "%s %s decision %d: %s%s" % (
            t.display(by), verdict, d["id"], " ".join(d["what"].split())[:300], ("\nNote: " + note) if note else ""),
            thread=d["thread"], mirror=False)
    return dict(d, status=verdict)


def email_approval(text, decisions, mail_ids):
    """Which pending emails a human's message approves, given what else waits in the conversation.
    Returns (ids or None, note): ids is the list to approve (None: none); note explains an ambiguity.
    - "approve" with only emails pending: all of them; "approve 12 13": those.
    - "approve" with a PR or another decision also pending is ambiguous: no email is approved, the note says why.
    - "approve the emails" / "send it" approve them; bare numbers count only when they are all pending email ids."""
    if not APPROVE_RE.match(text or "") or not mail_ids:
        return None, None
    nums = [int(n) for n in re.findall(r"\b(\d+)\b", text)]
    if decisions and not EMAILS_RE.search(text):
        if nums and not set(nums) <= set(mail_ids):   # "approve 48" with PR #48 pending: the PR, not an email
            return None, None
        if not nums:
            return None, "[bot: \"%s\" is ambiguous: %d email(s) and %d decision(s) wait here; no email was approved]" % (
                text.strip(), len(mail_ids), len(decisions))
    return (nums or sorted(mail_ids)), None


# ---- promises -------------------------------------------------------------------------------------------------------

def due_at(text, now=None):
    """"14:00" (today, or tomorrow if past), "+90m", "+2h", "2026-10-22 07:00" → epoch seconds."""
    t, now = str(text).strip(), now or time.time()
    m = re.fullmatch(r"\+(\d+)\s*([mh])", t)
    if m:
        return now + int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        lt = time.localtime(now)
        at = time.mktime(lt[:3] + (int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return at if at > now else at + 86400
    try:
        return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M"))
    except ValueError:
        raise ValueError("`due` like \"14:00\", \"+90m\", \"+2h\" or \"2026-10-22 07:00\".") from None


def promised(c, agent, tid, text):
    """Refuses a message that promises something for later when the agent has no open promise in this conversation."""
    m = PROMISE_RE.search(text or "")
    if not m or config.team().is_human(agent):
        return
    if not c.execute("SELECT 1 FROM promises WHERE agent = ? AND status = 'open' AND coalesce(thread, -1) = coalesce(?, -1)",
                     (agent, tid)).fetchone():
        raise ValueError("This promises something for later (\"%s\"). Register it first with `commit` (what, due, depends), "
                         "so you're woken for it and whoever asked gets the outcome; then send this again." % m.group(0))


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
