"""Plays a scripted conversation into a throwaway mailbox, in a loop, so the office can be shown without a real team.

The script is JSON: {"loop_pause": seconds, "events": [...]}, each event with "at" (seconds from the start) and one of:
  {"msg": {"conv": "c1", "from": "owner", "to": "lead", "kind": "task", "text": "..."}}   a message (conv groups a thread)
  {"start": "lead"}                         the agent starts a run (its waiting messages are read)
  {"do": "lead", "text": "reads orders.csv"} one line of what it's doing
  {"end": "lead", "tokens": 12000, "cost": 0.05}   the run ends
Nothing in a replay is real: no Claude, no network, no files outside a temp folder."""
import json
import time

from . import config, store


def load(path):
    with open(path) as f:
        script = json.load(f)
    t = config.team()
    for ev in script["events"]:
        who = ev.get("start") or ev.get("do") or ev.get("end")
        m = ev.get("msg")
        for x in ([who] if who else []) + ([m["from"], m["to"]] if m else []):
            if x not in t.members and x != "system":
                raise ValueError("replay: %r is not on the team in %s" % (x, t.path))
    script["events"].sort(key=lambda e: e["at"])
    return script


def wipe(c):
    for table in ("messages", "runs", "activity", "state", "outbox"):
        c.execute("DELETE FROM %s" % table)
    store.state(c, "replay", 1)


def play_once(c, script, speed=1.0):
    convs, runs = {}, {}
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
