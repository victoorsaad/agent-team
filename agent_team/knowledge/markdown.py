"""A folder of Markdown notes (an Obsidian vault works as is). Reads are confined to the folder; the daily note is the
only thing written, and with `git: true` it is committed and pushed alone."""
import os
import re
import subprocess

from .. import config

name = "markdown"
SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules"}


def root():
    k = config.team().knowledge
    p = k.get("path") or config.setting("TEAM_KNOWLEDGE_PATH")
    return os.path.realpath(os.path.expanduser(p if os.path.isabs(p) else os.path.join(config.team().base, p)))


def resolve(ref):
    """A note's path inside the folder ("Team/Context" or "Team/Context.md" or "[[Context]]"), or ValueError."""
    ref = ref.strip().strip("[]").split("|")[0]
    base = root()
    cand = os.path.realpath(os.path.join(base, ref if ref.endswith(".md") else ref + ".md"))
    if not cand.startswith(base + os.sep):
        raise ValueError("Outside the notes folder: %s" % ref)
    if os.path.isfile(cand):
        return cand
    # Obsidian-style: [[Name]] finds Name.md anywhere in the vault
    want = os.path.basename(cand).lower()
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if f.lower() == want:
                return os.path.join(d, f)
    raise ValueError("No note %s" % ref)


def read(ref):
    path = resolve(ref)
    with open(path, encoding="utf-8", errors="replace") as f:
        return os.path.relpath(path, root()), f.read()


def search(query, limit=10):
    """Notes whose name or text has every word of `query` (case-insensitive), names first; one line of context each."""
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        return []
    base, hits = root(), []
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(d, f)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(400_000)
            except OSError:
                continue
            low = text.lower()
            if all(w in low or w in f.lower() for w in words):
                line = next((ln.strip() for ln in text.splitlines() if words[0] in ln.lower()), "")
                hits.append((0 if all(w in f.lower() for w in words) else 1, os.path.relpath(path, base), line[:200]))
    hits.sort()
    return [{"ref": r, "line": ln} for _, r, ln in hits[:limit]]


def write_daily(day, title, text):
    k = config.team().knowledge
    rel = os.path.join(k.get("daily_dir", "Team/Daily"), day.strftime("%Y-%m-%d") + ".md")
    path = os.path.realpath(os.path.join(root(), rel))
    if not path.startswith(root() + os.sep):
        raise ValueError("daily_dir points outside the notes folder")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = k.get("header", "")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("# %s\n\n%s%s\n" % (title, (header.format(date=day.strftime("%Y-%m-%d")) + "\n\n") if header else "", text.strip()))
    os.replace(path + ".tmp", path)
    if k.get("git"):
        git = lambda *a: subprocess.run(["git", "-C", root(), *a], capture_output=True, text=True, timeout=120)   # noqa: E731
        git("pull", "-q", "--rebase", "--autostash")
        git("add", "--", rel)
        if git("diff", "--cached", "--quiet", "--", rel).returncode:
            git("commit", "-q", "-m", "%s: daily note %s" % (re.sub(r"\s+", " ", config.team().name), day.strftime("%Y-%m-%d")), "--", rel)
            out = git("push", "-q")
            if out.returncode:
                print("knowledge push", out.stderr[:200], flush=True)
    return rel
