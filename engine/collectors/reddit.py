"""Reddit posts and comments.

Primary: Arctic Shift (free Reddit archive API, no key) - pulls every new post and comment in each
client community since the last run, then the matcher filters locally (full recall, few requests).
Fallback: Reddit RSS (/r/SUB/new/.rss) when Arctic Shift returns nothing for a subreddit.
Plus: site-wide Reddit search RSS for the strongest explicit-intent phrases, to catch posts outside
the community list.
"""
from __future__ import annotations

import time
from urllib.parse import quote_plus

import feedparser

from .. import db
from ..config import union_list
from ..http import http
from ..item import Item
from ..util import clean_text, company_domains_in_text, from_epoch, parse_date

NAME = "reddit"
AS = "https://arctic-shift.photon-reddit.com/api"
MAX_PAGES = 6
LOOKBACK_FIRST_RUN = 2 * 86400

SITEWIDE_QUERIES = [
    '"looking for" "lead generation"', '"recommend" "lead generation agency"', '"outbound agency"',
    '"cold email agency"', '"appointment setting" agency', '"outsourced SDR"', '"appointment setter" hire',
    '"looking for" "appointment setter"', '"need more leads" B2B', '"struggling to get clients" B2B',
]


def _post_item(d: dict) -> Item:
    body = clean_text(d.get("selftext"), 6000)
    title = clean_text(d.get("title"), 500)
    return Item(
        source="reddit", source_id=f"t3_{d['id']}", kind="post",
        url=f"https://www.reddit.com{d.get('permalink') or '/comments/' + d['id']}",
        title=title, body=body, author=d.get("author"),
        company_domain=(company_domains_in_text(f"{body} {d.get('url') or ''}") or [None])[0],
        published_at=from_epoch(d.get("created_utc")),
        meta={"subreddit": (d.get("subreddit") or "").lower(), "score": d.get("score"),
              "num_comments": d.get("num_comments"), "flair": d.get("link_flair_text")},
    )


def _comment_item(d: dict) -> Item:
    body = clean_text(d.get("body"), 4000)
    link_id = (d.get("link_id") or "").replace("t3_", "")
    return Item(
        source="reddit", source_id=f"t1_{d['id']}", kind="comment",
        url=f"https://www.reddit.com/comments/{link_id}/_/{d['id']}/",
        title=None, body=body, author=d.get("author"),
        company_domain=(company_domains_in_text(body) or [None])[0],
        published_at=from_epoch(d.get("created_utc")),
        meta={"subreddit": (d.get("subreddit") or "").lower(), "score": d.get("score"), "link_id": link_id},
    )


def _arctic(kind: str, sub: str, after: int, errors: list[str]) -> list[dict]:
    out: list[dict] = []
    cursor = after
    for _ in range(MAX_PAGES):
        try:
            r = http.json(f"{AS}/{kind}/search", params={
                "subreddit": sub, "after": cursor, "limit": 100, "sort": "asc"}, timeout=40)
        except Exception as e:
            errors.append(f"arctic {kind} r/{sub}: {e}")
            break
        data = r.get("data") or []
        if not data:
            break
        out.extend(data)
        last = max(int(d.get("created_utc") or 0) for d in data)
        if len(data) < 100 or last <= cursor:
            break
        cursor = last
    return out


def _rss(url: str, errors: list[str]) -> list[Item]:
    items: list[Item] = []
    try:
        r = http.get(url, browser=True, timeout=30, retries=1)
        feed = feedparser.parse(r.content)
    except Exception as e:
        errors.append(f"rss {url[:80]}: {e}")
        return items
    for e in feed.entries:
        eid = (e.get("id") or e.get("link") or "").split("/")[-1] or e.get("link")
        body = clean_text(e.get("summary"), 5000)
        sub = ""
        for t in e.get("tags", []) or []:
            sub = (t.get("term") or "").lower()
        items.append(Item(
            source="reddit", source_id=eid if eid.startswith("t3_") else f"t3_{eid}", kind="post",
            url=e.get("link"), title=clean_text(e.get("title"), 500), body=body,
            author=(e.get("author") or "").replace("/u/", ""),
            company_domain=(company_domains_in_text(body) or [None])[0],
            published_at=parse_date(e.get("updated") or e.get("published")), meta={"subreddit": sub, "via": "rss"},
        ))
    return items


def _sitewide(errors: list[str]) -> list[Item]:
    """Explicit-intent phrases across ALL of Reddit (outside the community list) via Arctic Shift full-text search."""
    out: list[Item] = []
    after = int(time.time()) - 3 * 86400
    for qy in SITEWIDE_QUERIES:
        try:
            r = http.json(f"{AS}/posts/search", params={"query": qy.replace('"', ''), "after": after, "limit": 100},
                          timeout=40, retries=0)
            out.extend(_post_item(d) for d in r.get("data") or [] if d.get("id"))
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            errors.append(f"sitewide '{qy}': {code or e}")
            if code == 400:  # API refuses search without a subreddit; stop trying this run
                break
    return out


def collect():
    errors: list[str] = []
    items: list[Item] = []
    now = int(time.time())
    state = db.kv_get("reddit_cursor", {}) or {}
    for sub in union_list("communities"):
        key = sub.lower()
        after = int(state.get(key, now - LOOKBACK_FIRST_RUN))
        after = max(after, now - 7 * 86400)
        posts = _arctic("posts", sub, after, errors)
        comments = _arctic("comments", sub, after, errors)
        if posts or comments:
            items.extend(_post_item(d) for d in posts if d.get("id"))
            items.extend(_comment_item(d) for d in comments if d.get("id") and d.get("body") not in ("[deleted]", "[removed]")
                         and d.get("author") != "AutoModerator")
            newest = max([int(d.get("created_utc") or 0) for d in posts + comments] + [after])
            state[key] = newest
        else:
            items.extend(_rss(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100", errors))
    items.extend(_sitewide(errors))
    db.kv_set("reddit_cursor", state)
    return items, errors
