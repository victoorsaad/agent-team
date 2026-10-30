"""Notion through its REST API (standard library). NOTION_TOKEN is an internal integration's secret; share with it only
the pages the team may read, plus the parent of the daily notes (it needs "insert content" there).

team.json: "read": [page ids or URLs], "daily_parent": a page id (each day becomes a child page) or "daily_database":
a database id (each day becomes a row; its title property is found by type)."""
import json
import re
import urllib.request

from .. import config

API = "https://api.notion.com/v1/"
VERSION = "2022-06-28"
name = "notion"


def call(method, path, payload=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"authorization": "Bearer " + config.setting("NOTION_TOKEN"),
                                          "notion-version": VERSION, "content-type": "application/json",
                                          "user-agent": "agent-team/0.3"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def page_id(ref):
    """A page id from an id or a notion.so URL (the 32 hex characters at the end)."""
    m = re.findall(r"(?<![0-9a-f])[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}(?![0-9a-f])", ref.lower())
    if not m:
        raise ValueError("Not a Notion page id or URL: %s" % ref)
    return m[-1].replace("-", "")


def plain(rich):
    return "".join(x.get("plain_text", "") for x in rich or [])


PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ", "numbered_list_item": "1. ",
          "quote": "> ", "callout": "> ", "to_do": "- [ ] ", "toggle": "- "}


def blocks_text(block_id, depth=0, budget=None):
    """A page's blocks as Markdown-ish text (one level of children), at most ~budget characters."""
    budget = budget if budget is not None else [40_000]
    out, cursor = [], None
    while budget[0] > 0:
        res = call("GET", "blocks/%s/children?page_size=100%s" % (block_id, "&start_cursor=" + cursor if cursor else ""))
        for b in res.get("results") or []:
            kind = b.get("type")
            body = b.get(kind) or {}
            if kind == "child_page":
                line = "[page] %s (%s)" % (body.get("title", ""), b["id"].replace("-", ""))
            elif kind == "code":
                line = "```\n%s\n```" % plain(body.get("rich_text"))
            elif "rich_text" in body:
                pre = "- [x] " if kind == "to_do" and body.get("checked") else PREFIX.get(kind, "")
                line = pre + plain(body.get("rich_text"))
            else:
                continue
            out.append("  " * depth + line)
            budget[0] -= len(line)
            if b.get("has_children") and depth < 1 and kind != "child_page":
                out.append(blocks_text(b["id"], depth + 1, budget))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return "\n".join(x for x in out if x)


def title_of(page):
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain(prop.get("title")) or "Untitled"
    return "Untitled"


def read(ref):
    pid = page_id(ref)
    return title_of(call("GET", "pages/%s" % pid)), blocks_text(pid)


def search(query, limit=10):
    res = call("POST", "search", {"query": query, "page_size": min(limit, 50), "filter": {"property": "object", "value": "page"}})
    return [{"ref": p["id"].replace("-", ""), "line": title_of(p)} for p in (res.get("results") or [])[:limit]]


def rich(text):
    return [{"type": "text", "text": {"content": text[i:i + 1900]}} for i in range(0, len(text), 1900)] or [{"type": "text", "text": {"content": ""}}]


def md_blocks(text):
    """The daily note's Markdown → Notion blocks (headings, bullets, quotes, paragraphs; a table row stays a paragraph)."""
    out = []
    for ln in text.splitlines():
        if not ln.strip() or re.fullmatch(r"\|?[-\s|:]+\|?", ln.strip()):
            continue
        for pre, kind in (("### ", "heading_3"), ("## ", "heading_2"), ("# ", "heading_1"), ("- ", "bulleted_list_item"), ("> ", "quote")):
            if ln.startswith(pre):
                out.append({"object": "block", "type": kind, kind: {"rich_text": rich(ln[len(pre):])}})
                break
        else:
            out.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(ln)}})
    return out[:100]   # Notion takes up to 100 children per request


def write_daily(day, title, text):
    k = config.team().knowledge
    blocks = md_blocks(text)
    if k.get("daily_database"):
        db = call("GET", "databases/%s" % page_id(k["daily_database"]))
        prop = next(n for n, p in db["properties"].items() if p.get("type") == "title")
        page = call("POST", "pages", {"parent": {"database_id": db["id"]}, "properties": {prop: {"title": rich(title)}}, "children": blocks})
    elif k.get("daily_parent"):
        page = call("POST", "pages", {"parent": {"page_id": page_id(k["daily_parent"])},
                                      "properties": {"title": {"title": rich(title)}}, "children": blocks})
    else:
        raise ValueError("knowledge (notion) needs daily_parent or daily_database to write the daily note")
    return page.get("url") or page.get("id")
