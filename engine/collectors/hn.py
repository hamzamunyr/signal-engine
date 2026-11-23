"""Hacker News via the Algolia API (no key): keyword posts and comments, plus the monthly
"Who is hiring" thread (parsed into job items)."""
from __future__ import annotations

import re
import time

from .. import db
from ..config import union_list
from ..http import http
from ..item import Item
from ..util import clean_text, company_domains_in_text, from_epoch

NAME = "hn"
API = "https://hn.algolia.com/api/v1"


def _hit_item(h: dict) -> Item:
    is_comment = "comment" in (h.get("_tags") or [])
    body = clean_text(h.get("comment_text") or h.get("story_text"), 5000)
    title = clean_text(h.get("title") or h.get("story_title"), 400)
    url = f"https://news.ycombinator.com/item?id={h['objectID']}"
    return Item(
        source="hn", source_id=str(h["objectID"]), kind="comment" if is_comment else "post",
        url=url, title=None if is_comment else title, body=body, author=h.get("author"),
        company_domain=(company_domains_in_text(f"{body} {h.get('url') or ''}") or [None])[0],
        published_at=from_epoch(h.get("created_at_i")),
        meta={"story_title": title, "points": h.get("points"), "ext_url": h.get("url")},
    )


def _hiring_items(errors: list[str]) -> list[Item]:
    """Parse the latest 'Ask HN: Who is hiring?' thread into job items (first line: Company | Role | Place)."""
    out: list[Item] = []
    try:
        r = http.json(f"{API}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 3})
        story = next((h for h in r.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
        if not story:
            return out
        page = 0
        while page < 10:
            c = http.json(f"{API}/search", params={"tags": f"comment,story_{story['objectID']}",
                                                    "hitsPerPage": 1000, "page": page})
            for h in c.get("hits", []):
                if h.get("parent_id") != int(story["objectID"]):
                    continue  # only top-level comments are job posts
                raw = h.get("comment_text") or ""
                first = clean_text(raw.split("<p>")[0], 300)
                parts = [p.strip() for p in re.split(r"\s+\|\s+", first)]
                company = parts[0] if parts else None
                body = clean_text(raw, 4000)
                out.append(Item(
                    source="hn_hiring", source_id=str(h["objectID"]), kind="job",
                    url=f"https://news.ycombinator.com/item?id={h['objectID']}",
                    title=" | ".join(parts[1:3]) if len(parts) > 1 else first, body=body,
                    company_name=company, company_domain=(company_domains_in_text(raw) or [None])[0],
                    published_at=from_epoch(h.get("created_at_i")),
                    meta={"board": "hn", "header": first},
                ))
            if page + 1 >= c.get("nbPages", 1):
                break
            page += 1
    except Exception as e:
        errors.append(f"who is hiring: {e}")
    return out


def collect():
    errors: list[str] = []
    items: list[Item] = []
    now = int(time.time())
    since = int(db.kv_get("hn_since", now - 2 * 86400) or (now - 2 * 86400))
    since = max(since, now - 7 * 86400)
    for qy in union_list("hn_queries"):
        try:
            r = http.json(f"{API}/search_by_date", params={
                "query": qy, "tags": "(story,comment)", "hitsPerPage": 100,
                "numericFilters": f"created_at_i>{since}"})
            items.extend(_hit_item(h) for h in r.get("hits", []))
        except Exception as e:
            errors.append(f"hn '{qy}': {e}")
    # Ask HN posts are where founders ask for help; take all recent ones.
    try:
        r = http.json(f"{API}/search_by_date", params={
            "tags": "ask_hn", "hitsPerPage": 200, "numericFilters": f"created_at_i>{since}"})
        items.extend(_hit_item(h) for h in r.get("hits", []))
    except Exception as e:
        errors.append(f"ask_hn: {e}")
    # Who is hiring: once a day is enough
    last_hiring = int(db.kv_get("hn_hiring_at", 0) or 0)
    if now - last_hiring > 20 * 3600:
        items.extend(_hiring_items(errors))
        db.kv_set("hn_hiring_at", now)
    db.kv_set("hn_since", now - 600)
    return items, errors
