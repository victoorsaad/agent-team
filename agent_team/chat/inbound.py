"""What the chat bots do with a human's message, whatever the chat (discord_bot.py, slack_bot.py call this):

- A new message in the channel starts a conversation: "marketing, what's the angle for Friday?" goes to that agent;
  without a name it goes to the orchestrator.
- Inside a thread, just answer: it goes to the agent who spoke last there (or to the one you name). With
  "single_door": true it goes to the orchestrator instead, who knows what's pending there and answers with a receipt.
- "approve" (or "approve 12") in a thread approves the emails pending in that conversation; the mailer re-checks it
  against the chat itself. With a PR or another decision also pending there, a plain "approve" is ambiguous and
  approves no email: say "approve the emails" or their numbers (bare numbers count only when they are pending emails).
- A loose "approve" (a new message, or a thread with nothing pending) goes to the only decision waiting for a human,
  in that decision's conversation, when exactly one waits and no email does anywhere. Otherwise nothing is approved.
- A message from a monitor (a webhook or bot whose name is in team.json alerts.names) becomes a task for the
  orchestrator, whose answer comes back to the humans in that thread."""
import re

from .. import config, store


def kind_of(body):
    return "ask" if body.rstrip().endswith("?") else "task"


def namer():
    """A function text → (agent named at the start or None, the rest)."""
    t = config.team()
    names = {}
    for a, r in t.roles.items():
        names[a.lower()] = a
        names[r.get("name", a).lower()] = a
    rx = re.compile(r"^\s*@?(%s)\b[:,]?\s*" % "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)

    def named(text):
        m = rx.match(text)
        return (names[m.group(1).lower()], text[m.end():]) if m else (None, text)
    return named


def approve_emails(c, text, tid, ref, via):
    """A human's message in a conversation → the pending emails it approves (store.email_approval decides which).
    `ref` is the chat message id, `via` the provider: the mailer reads that message back before sending.
    Returns (warning for the human or None, note for the agent who gets the message or None)."""
    dec, mails = store.pending(c, tid)
    ids, note = store.email_approval(text, dec, [m["id"] for m in mails])
    if ids is None:
        return None, note
    q = ",".join("?" * len(ids))
    done = c.execute("UPDATE outbox SET status = 'approved', approved_by = ?, approval_ref = ? WHERE thread = ? "
                     "AND status = 'pending' AND id IN (%s)" % q, [via, str(ref), tid] + ids).rowcount
    note = "[bot: %d email(s) approved by this message; the mailer sends them]" % done if done else None
    missing = sorted(set(ids) - {m["id"] for m in mails})
    if missing:
        return ("Email no. %s isn't pending in this conversation: approve it in the thread where it was shown."
                % ", ".join(map(str, missing))), note
    return None, note


LOOSE_RE = re.compile(r"^\s*(approve[ds]?|aprov)", re.I)
PR_RE = re.compile(r"\b(the\s+|o\s+)?PR\b(\s*#?\s*\d+)?", re.I)


def loose_approval(c, text, tid=None):
    """The one pending decision a loose "approve" means, or None. Loose: the message is a bare approval (no emails, no
    numbers but a PR's) said where nothing is pending; it counts only when exactly one decision waits in the whole
    team and no email does. "ok" or "yes" alone are small talk, never this."""
    core = PR_RE.sub(" ", text or "").strip()
    if not LOOSE_RE.match(text or "") or not store.APPROVE_RE.match(core) or store.EMAILS_RE.search(text) or re.search(r"\d", core):
        return None
    if tid is not None and any(store.pending(c, tid)):
        return None
    if c.execute("SELECT 1 FROM outbox WHERE status = 'pending' LIMIT 1").fetchone():
        return None
    dec = c.execute("SELECT * FROM decisions WHERE status = 'pending' LIMIT 2").fetchall()
    if len(dec) != 1 or (tid is not None and dec[0]["thread"] == tid):
        return None
    d = dec[0]
    pr = PR_RE.search(text)
    num = re.search(r"\d+", pr.group()) if pr else None
    if pr and (d["kind"] != "pr" or (num and d["ref"] != "pr:%s" % num.group())):
        return None   # "approve the PR" with only another kind (or another PR) pending: not this one
    return d


def answer(c, me, to, body, tid):
    """A human's message inside a conversation. `to` is the agent they named, if any. Without a name: with a single
    door, the orchestrator; otherwise the agent who spoke last there."""
    t = config.team()
    humans = list(t.humans)
    q = ",".join("?" * len(humans))
    if t.single_door:
        to = to or t.orchestrator
        last = c.execute("SELECT id FROM messages WHERE thread = ? AND sender NOT IN (%s) AND sender <> 'system' "
                         "AND (sender = ? OR recipient IN (%s)) ORDER BY id DESC LIMIT 1" % (q, q), [tid] + humans + [to] + humans).fetchone()
        if last:
            c.execute("UPDATE messages SET status = 'done' WHERE id = ? AND status <> 'done' AND recipient IN (%s)" % q, [last["id"]] + humans)
            return store.send(c, me, to, "reply", body, reply_to=last["id"], mirror=False)
        return store.send(c, me, to, kind_of(body), body, mirror=False, thread=tid)
    last = c.execute("SELECT id, sender FROM messages WHERE thread = ? AND sender NOT IN (%s) AND sender <> 'system' " % q
                     + ("AND sender = ? " if to else "") + "ORDER BY id DESC LIMIT 1",
                     [tid] + humans + ([to] if to else [])).fetchone()
    if last:
        c.execute("UPDATE messages SET status = 'done' WHERE id = ? AND status <> 'done'", (last["id"],))
        return store.send(c, me, last["sender"], "reply", body, reply_to=last["id"], mirror=False)
    return store.send(c, me, to or t.orchestrator, kind_of(body), body, mirror=False, thread=tid)


def handle(c, me, text, said, tid, ref, via):
    """One message from human `me`. `text` is what agents get (attachments listed), `said` what the human typed,
    `tid` the conversation of the chat thread it was said in (None: a new message in the channel), `ref` the chat
    message id. Returns (replies for the bot to post, id of the conversation it started or None)."""
    t = config.team()
    named = namer()
    replies = []
    d = loose_approval(c, said, tid)
    if d:   # the human's word goes to the decision's own conversation, where it's recorded with `decide`
        to, body = named(text)
        answer(c, me, to, body + "\n\n[bot: \"%s\" was said outside this conversation, where nothing was pending; it's "
               "the team's only open decision, so it came here]" % said.strip(), d["thread"])
        replies.append("Taken as approval of the only pending decision: %s" % " ".join(d["what"].split())[:300])
        return replies, None
    if tid:
        warn, note = approve_emails(c, said, tid, ref, via)
        if warn:
            replies.append(warn)
        to, body = named(text)
        answer(c, me, to, body + ("\n\n" + note if note else ""), tid)
        return replies, None
    to, body = named(text)
    return replies, store.send(c, me, to or t.orchestrator, kind_of(body), body, mirror=False)


def is_alert_sender(name):
    """A webhook or bot name from team.json alerts.names (a monitor, not a person)."""
    return bool(name) and name in (config.team().alerts.get("names") or [])
