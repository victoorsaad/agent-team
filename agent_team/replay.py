"""Plays a scripted conversation into a throwaway mailbox, in a loop, so the office can be shown without a real team.

The script is JSON: {"loop_pause": seconds, "events": [...]}, each event with "at" (seconds from the start) and one of:
  {"msg": {"conv": "c1", "from": "owner", "to": "lead", "kind": "task", "text": "..."}}   a message (conv groups a thread)
  {"start": "lead"}                         the agent starts a run (its waiting messages are read)
  {"do": "lead", "text": "reads orders.csv"} one line of what it's doing
  {"end": "lead", "tokens": 12000, "cost": 0.05}   the run ends
  {"decision": {"conv": "c1", "id": "d1", "from": "ops", "owner": "reviewer", "kind": "pr", "ref": "pr:48", "text": "..."}}
                                            a decision opens (and its message reaches the approvers)
  {"decide": "d1", "verdict": "approved", "by": "ana", "via": "ops"}   a human's answer, recorded by the orchestrator
  {"done": {"conv": "c1", "agent": "reviewer", "text": "PR #48 merged", "proof": "https://…"}}   a line in "Done today"
  {"promise": {"conv": "c1", "id": "p1", "agent": "ops", "what": "...", "due": "+3h"}}   a promise (`commit`)
  {"kept": "p1", "proof": "…"}             the promise is closed (`commit_done`)
  {"digest": true}                          the daily digest, assembled from the replay's own tables
Nothing in a replay is real: no Claude, no network, no files outside a temp folder."""
import json
import time

from . import config, store


def load(path):
    with open(path) as f:
        script = json.load(f)
    t = config.team()
    for ev in script["events"]:
        who = ev.get("start") or ev.get("do") or ev.get("end") or (ev.get("done") or ev.get("promise") or {}).get("agent") or ev.get("by")
        m = ev.get("msg") or ev.get("decision")
        for x in ([who] if who else []) + ([m["from"], m.get("to", m["from"])] if m else []):
            if x not in t.members and x != "system":
                raise ValueError("replay: %r is not on the team in %s" % (x, t.path))
    script["events"].sort(key=lambda e: e["at"])
    return script


def wipe(c):
    for table in ("messages", "runs", "activity", "state", "outbox", "decisions", "promises", "done"):
        c.execute("DELETE FROM %s" % table)
    store.state(c, "replay", 1)


def play_once(c, script, speed=1.0):
    convs, runs, decisions, promises = {}, {}, {}, {}
    t0 = time.time()
    for ev in script["events"]:
        delay = t0 + ev["at"] / speed - time.time()
        if delay > 0:
            time.sleep(delay)
        if "msg" in ev:
            m = ev["msg"]
            tid = convs.get(m.get("conv"))
            mid = store.send(c, m["from"], m["to"], m.get("kind", "ask"), m["text"], thread=tid, mirror=False)
            if m.get("conv") and tid is None:
                convs[m["conv"]] = mid
        elif "decision" in ev:
            d = ev["decision"]
            did, mid = store.open_decision(c, d["from"], d.get("kind", "other"), d["text"], thread=convs.get(d.get("conv")), ref=d.get("ref"),
                                             owner=d.get("owner"), mirror=False)
            decisions[d.get("id")] = did
            if d.get("conv") and d["conv"] not in convs:
                convs[d["conv"]] = mid
        elif "decide" in ev:
            store.decide(c, decisions[ev["decide"]], ev.get("verdict", "approved"), ev["by"], sender=ev.get("via"))
        elif "done" in ev:
            d = ev["done"]
            store.did(c, d["agent"], d["text"], proof=d.get("proof"), thread=convs.get(d.get("conv")))
        elif "promise" in ev:
            p = ev["promise"]
            promises[p.get("id")] = c.execute("INSERT INTO promises (agent, thread, what, due, created_at) VALUES (?, ?, ?, ?, ?)",
                                              (p["agent"], convs.get(p.get("conv")), p["what"], store.due_at(p.get("due", "+3h")), time.time())).lastrowid
        elif "kept" in ev:
            pid = promises[ev["kept"]]
            c.execute("UPDATE promises SET status = 'done', proof = ?, closed_at = ? WHERE id = ?", (ev.get("proof"), time.time(), pid))
        elif "digest" in ev:
            from .dispatcher import digest_text
            t = config.team()
            text = digest_text(c)
            if text:
                store.send(c, t.orchestrator, t.primary_human, "digest", text, mirror=False)
        elif "start" in ev:
            a = ev["start"]
            rows = store.inbox(c, a)
            if rows:
                tid = rows[0]["thread"] or rows[0]["id"]
                store.state(c, "subject:" + a, " ".join(c.execute("SELECT body FROM messages WHERE id = ?", (tid,)).fetchone()["body"].split())[:160])
                store.state(c, "thread:" + a, tid)
            runs[a] = c.execute("INSERT INTO runs (agent, started_at) VALUES (?, ?)", (a, time.time())).lastrowid
        elif "do" in ev:
            c.execute("INSERT INTO activity (run_id, agent, ts, text) VALUES (?, ?, ?, ?)",
                      (runs.get(ev["do"]), ev["do"], time.time(), ev["text"][:200]))
        elif "end" in ev:
            a = ev["end"]
            th = store.state(c, "thread:" + a)
            c.execute("UPDATE runs SET ended_at = ?, ok = 1, thread = ?, tokens_in = ?, tokens_out = ?, cost_usd = ? WHERE id = ?",
                      (time.time(), int(th) if th else None, int(ev.get("tokens", 0) * .9), int(ev.get("tokens", 0) * .1),
                       ev.get("cost", 0), runs.pop(a, None)))
            c.execute("UPDATE messages SET status = 'done' WHERE recipient = ? AND status = 'read'", (a,))
    for a, rid in runs.items():   # a script that forgot an "end"
        c.execute("UPDATE runs SET ended_at = ?, ok = 1 WHERE id = ?", (time.time(), rid))


def play(path, db_path, speed=1.0, loops=None):
    script = load(path)
    c = store.db(db_path)
    n = 0
    while loops is None or n < loops:
        wipe(c)
        play_once(c, script, speed)
        n += 1
        time.sleep(script.get("loop_pause", 8) / speed)
