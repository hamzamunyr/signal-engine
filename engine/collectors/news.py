"""News: Google News RSS searches, startup/funding RSS feeds and press-release wires (all free, no key).
Feeds funding (42-51), leadership (52-61), growth/launch (62-68, 107) and timing (112-114) signals."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import quote_plus

import feedparser

from ..config import union_list
from ..http import http
from ..item import Item
from ..util import clean_text, parse_date

NAME = "news"
RSS_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"

GLOBAL_QUERIES = [
    '"raises" "pre-seed"', '"pre-seed round"', '"seed round" raises', '"raises" seed startup',
    '"Series A" raises', '"secures" funding startup', '"closes" seed round',
    '"appoints" "VP of Sales"', '"names" "Head of Sales"', '"joins" "as Head of Sales"',
    '"appoints" "Chief Revenue Officer"', '"Head of Growth" joins', '"appoints" "Chief Marketing Officer" startup',
    '"first sales hire"', '"launches" startup B2B', '"expands into" startup', '"opens new office" startup',
    '"strategic partnership" startup', '"new CEO" startup',
]

FEEDS = {
    "techcrunch_startups": "https://techcrunch.com/category/startups/feed/",
    "techcrunch_funding": "https://techcrunch.com/tag/funding/feed/",
    "crunchbase_news": "https://news.crunchbase.com/feed/",
    "eu_startups": "https://www.eu-startups.com/feed/",
    "prnewswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "businesswire": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ==",
    "accesswire": "https://www.accesswire.com/users/newswire/rss",
}

VERBS = r"(raises|raised|secures|closes|lands|appoints|names|hires|launches|announces|acquires|acquired|partners|" \
        r"expands|opens|unveils|introduces|welcomes|taps|completes|debuts|bags|nabs|snags|gets|receives|rolls out|enters)"
COMPANY_RE = re.compile(r"^(?:[\w\-]+-based |.{0,40}startup )?(.{2,60}?)\s+" + VERBS + r"\b", re.I)
AMOUNT_RE = re.compile(r"[\$€£]\s?\d+(?:[\.,]\d+)?\s?(?:m|million|k|thousand|b|billion|mn)\b", re.I)


def guess_company(title: str) -> str | None:
    m = COMPANY_RE.search(title or "")
    if not m:
        return None
    name = m.group(1).strip(" '\"“”,:-")
    name = re.sub(r"^(exclusive|breaking|report|update)\s*[:|-]\s*", "", name, flags=re.I)
    if len(name.split()) > 6:
        return None
    return name or None


def _entries(url: str, errors: list[str], label: str):
    try:
        r = http.get(url, browser=True, timeout=30, retries=1, headers={"Accept": RSS_ACCEPT})
        return feedparser.parse(r.content).entries
    except Exception as e:
        errors.append(f"{label}: {e}")
        return []


def _item(e, source: str, publisher: str | None = None) -> Item:
    title = clean_text(e.get("title"), 500)
    if source == "google_news" and " - " in title:
        title, publisher = title.rsplit(" - ", 1)
    link = e.get("link") or ""
    sid = hashlib.sha1((e.get("id") or link or title).encode()).hexdigest()[:20]
    body = clean_text(e.get("summary"), 2500)
    amt = AMOUNT_RE.search(f"{title} {body}")
    return Item(
        source=source, source_id=sid, kind="news", url=link, title=title, body=body,
        company_name=guess_company(title),
        published_at=parse_date(e.get("published") or e.get("updated")),
        meta={"publisher": publisher or source, "amount": amt.group(0) if amt else None},
    )


def collect():
    errors: list[str] = []
    items: list[Item] = []
    queries = GLOBAL_QUERIES + union_list("news_queries")
    for qy in queries:
        url = f"https://news.google.com/rss/search?q={quote_plus(qy + ' when:2d')}&hl=en-US&gl=US&ceid=US:en"
        for e in _entries(url, errors, f"gnews {qy}"):
            items.append(_item(e, "google_news"))
    for label, url in FEEDS.items():
        for e in _entries(url, errors, label):
            items.append(_item(e, label))
    return items, errors
