"""Knowledge: where the team's notes live, so agents read the context the humans keep (goals, product notes, how the
business works) and the team writes one note a day back (what got done, decided, promised and spent).

One provider per team, picked in team.json:

    "knowledge": {"provider": "markdown", "path": "/srv/notes", "read": ["Team/Context.md"], "daily_dir": "Team/Daily",
                  "git": true}
    "knowledge": {"provider": "notion", "read": ["<page id>"], "daily_parent": "<page id>"}

- markdown: any folder of .md files (an Obsidian vault works as is). `git: true` commits and pushes the daily note
  alone (the folder must be a clone the team's OS user can push).
- notion: the REST API with NOTION_TOKEN (an internal integration shared with the pages it may read or write).

`read` pages go into every agent's instructions (capped at knowledge.max_chars, default 12000); agents find the rest
with the `notes_search` / `notes_read` tools. Agents never write here directly: the daily note is assembled by code
from the mailbox (daily.py)."""
from .. import config


def provider():
    name = config.team().knowledge.get("provider")
    if name == "markdown":
        from . import markdown
        return markdown
    if name == "notion":
        from . import notion
        return notion
    return None


def enabled():
    return provider() is not None


def context():
    """The `read` pages as one block for the system prompt, or ""."""
    p = provider()
    if not p:
        return ""
    k = config.team().knowledge
    limit = int(k.get("max_chars", 12000))
    parts = []
    for ref in k.get("read") or []:
        try:
            title, text = p.read(ref)
        except Exception as e:  # noqa: BLE001
            print("knowledge read", ref, repr(e)[:200], flush=True)
            continue
        parts.append("## %s\n%s" % (title, text.strip()))
    text = "\n\n".join(parts)
    if not text:
        return ""
    if len(text) > limit:
        text = text[:limit] + "\n[…cut; use notes_read for the rest]"
    return ("# The humans' notes (%s)\nContext they keep for the team. Check here before asking them something it already "
            "answers; `notes_search` and `notes_read` find more.\n\n%s" % (k.get("label") or p.name, text))


def search(query, limit=10):
    p = provider()
    return p.search(query, limit) if p else []


def read(ref):
    p = provider()
    if not p:
        raise ValueError("No knowledge provider in team.json.")
    return p.read(ref)


def write_daily(day, title, text):
    """Writes (or replaces) the note of `day` (a date). Returns where it went."""
    p = provider()
    if not p:
        return None
    return p.write_daily(day, title, text)
