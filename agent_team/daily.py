"""The team's note of the day for the humans' notes (knowledge/): what got done, decided, promised and spent, built
from the mailbox by code (no agent run). Written once a day at knowledge.daily_at (default 23:50) by the dispatcher,
or by hand with `agent-team daily-note [YYYY-MM-DD]`."""
import time
from datetime import datetime

from . import config

L = {
    "en": {"title": "%s team · %s", "summary": "%d done, %d decided, US$ %.2f in %d runs.", "done": "Done",
           "decided": "Decided by the humans", "waiting": "Waiting for a human", "promises": "Promises",
           "until": "due %s", "cost": "Cost by agent", "cols": "| Agent | Runs | US$ |"},
    "pt": {"title": "Time %s · %s", "summary": "%d entregas, %d decisões, US$ %.2f em %d execuções.", "done": "Feito",
           "decided": "Decidido pelas pessoas", "waiting": "Esperando uma pessoa", "promises": "Promessas",
           "until": "até %s", "cost": "Custo por agente", "cols": "| Agente | Execuções | US$ |"},
}


def clip(t, n=220):
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def note(c, day):
    """(title, markdown) of `day` (a datetime at midnight)."""
    t = config.team()
    lab = L.get(t.language, L["en"])
    start = time.mktime(day.timetuple())
    end = start + 86400
    q = lambda sql: c.execute(sql, (start, end)).fetchall()   # noqa: E731
    cost = q("SELECT agent, count(*), round(sum(coalesce(cost_usd, 0)), 2) FROM runs WHERE started_at >= ? AND started_at < ? "
             "GROUP BY agent ORDER BY 3 DESC")
    done = q("SELECT ts, agent, origin, text, proof FROM done WHERE ts >= ? AND ts < ? ORDER BY ts")
    decided = q("SELECT decided_at, kind, ref, what, status, decided_by, note FROM decisions WHERE decided_at >= ? AND decided_at < ? "
                "ORDER BY decided_at")
    waiting = c.execute("SELECT kind, ref, what, owner FROM decisions WHERE status = 'pending' ORDER BY created_at").fetchall()
    closed = q("SELECT agent, what, status FROM promises WHERE closed_at >= ? AND closed_at < ? ORDER BY closed_at")
    open_p = c.execute("SELECT agent, what, due FROM promises WHERE status = 'open' ORDER BY due").fetchall()
    total = sum(r[2] or 0 for r in cost)
    out = [lab["summary"] % (len(done), len(decided), total, sum(r[1] for r in cost)), ""]
    if decided:
        out += ["## " + lab["decided"], ""] + ["- %s · %s %s · **%s** (%s): %s%s" % (
            hhmm(r[0]), r[1], r[2] or "", r[4], t.display(r[5] or "?"), clip(r[3]), (" — " + clip(r[6])) if r[6] else "") for r in decided] + [""]
    if waiting:
        out += ["## " + lab["waiting"], ""] + ["- %s %s (%s): %s" % (r[0], r[1] or "", t.display(r[3]), clip(r[2])) for r in waiting] + [""]
    if done:
        out += ["## " + lab["done"], ""] + ["- %s · %s (%s): %s%s" % (
            hhmm(r[0]), t.display(r[1]), r[2], clip(r[3]), (" — " + clip(r[4], 120)) if r[4] else "") for r in done] + [""]
    if closed or open_p:
        out += ["## " + lab["promises"], ""] + ["- %s %s: %s" % ("✅" if r[2] == "done" else "❌", t.display(r[0]), clip(r[1])) for r in closed]
        out += ["- ⏳ %s: %s (%s)" % (t.display(r[0]), clip(r[1]), lab["until"] % datetime.fromtimestamp(r[2]).strftime("%d/%m %H:%M"))
                for r in open_p] + [""]
    if cost:
        out += ["## " + lab["cost"], "", lab["cols"], "|---|---|---|"] + ["| %s | %d | %.2f |" % (t.display(a), n, v or 0) for a, n, v in cost] + [""]
    return lab["title"] % (t.name, day.strftime("%Y-%m-%d")), "\n".join(out)
