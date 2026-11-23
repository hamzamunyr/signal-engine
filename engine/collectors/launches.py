"""Product launches and new startups: Product Hunt feed, Show HN, BetaList, Launching Next.
Feeds signals 65, 78, 106, 107. A listing alone is context; the LLM decides if it is a lead."""
from __future__ import annotations

import hashlib
import time

import feedparser

from .. import db
from ..http import http
from ..item import Item
from ..util import clean_text, company_domains_in_text, from_epoch, parse_date, registrable_domain

NAME = "launches"

FEEDS = {
    "producthunt": "https://www.producthunt.com/feed",
    "betalist": "https://betalist.com/startups.rss",
    "launchingnext": "https://www.launchingnext.com/rss/",
}


def collect():
    errors: list[str] = []
    items: list[Item] = []
    for label, url in FEEDS.items():
        try:
            r = http.get(url, browser=True, timeout=30, retries=1)
            entries = feedparser.parse(r.content).entries
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
        for e in entries:
            title = clean_text(e.get("title"), 300)
            body = clean_text(e.get("summary") or e.get("description"), 2000)
            link = e.get("link") or ""
            sid = hashlib.sha1((e.get("id") or link).encode()).hexdigest()[:20]
            dom = (company_domains_in_text(e.get("summary") or "") or [None])[0]
            items.append(Item(
                source=label, source_id=sid, kind="launch", url=link, title=title, body=body,
                company_name=title.split(" - ")[0].split(" – ")[0][:80] if title else None, company_domain=dom,
                published_at=parse_date(e.get("published") or e.get("updated")),
                meta={"flags": ["launch_listing", "new_company_listing"] if label != "producthunt" else ["launch_listing"]},
            ))
    # Show HN
    now = int(time.time())
    since = int(db.kv_get("showhn_since", now - 2 * 86400) or (now - 2 * 86400))
    try:
        r = http.json("https://hn.algolia.com/api/v1/search_by_date", params={
            "tags": "show_hn", "hitsPerPage": 200, "numericFilters": f"created_at_i>{max(since, now - 7 * 86400)}"})
        for h in r.get("hits", []):
            title = clean_text(h.get("title"), 300).removeprefix("Show HN:").strip()
            ext = h.get("url") or ""
            items.append(Item(
                source="show_hn", source_id=str(h["objectID"]), kind="launch",
                url=f"https://news.ycombinator.com/item?id={h['objectID']}", title=title,
                body=clean_text(h.get("story_text"), 2000), author=h.get("author"),
                company_name=title.split(" – ")[0].split(" - ")[0][:80],
                company_domain=registrable_domain(ext) if ext and "github.com" not in ext else None,
                published_at=from_epoch(h.get("created_at_i")),
                meta={"flags": ["launch_listing"], "points": h.get("points"), "ext_url": ext},
            ))
        db.kv_set("showhn_since", now - 600)
    except Exception as e:
        errors.append(f"show_hn: {e}")
    return items, errors
