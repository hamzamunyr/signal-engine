"""SEC 8-K current filings: officer changes (5.02), acquisitions (2.01), material agreements (1.01),
restructuring (2.05). Public companies only, so most small-company clients skip this source."""
from __future__ import annotations

import re
from datetime import timedelta

import feedparser

from .. import db
from ..config import any_client_uses
from ..http import BOT_UA, http
from ..item import Item
from ..util import clean_text, now_utc, parse_date

NAME = "edgar_8k"
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
        "&start={start}&count=100&output=atom")
WATCH = {"5.02": "8k_5_02", "2.01": "8k_2_01", "1.01": "8k_1_01", "2.05": "8k_2_05"}
ITEM_RE = re.compile(r"Item\s+(\d\.\d\d)", re.I)
NAME_RE = re.compile(r"^8-K\s+-\s+(.+?)\s+\(\d+\)")


def collect():
    if not any_client_uses(NAME):
        return [], []
    errors: list[str] = []
    items: list[Item] = []
    since = parse_date(db.kv_get("8k_since")) or (now_utc() - timedelta(days=2))
    newest = since
    for start in range(0, 1000, 100):
        try:
            r = http.get(FEED.format(start=start), headers={"User-Agent": BOT_UA}, timeout=40)
            entries = feedparser.parse(r.content).entries
        except Exception as e:
            errors.append(f"feed: {e}")
            break
        if not entries:
            break
        stop = False
        for e in entries:
            updated = parse_date(e.get("updated"))
            if updated and updated <= since:
                stop = True
                continue
            newest = max(newest, updated or newest)
            summary = clean_text(e.get("summary"), 2000)
            found = {WATCH[i] for i in ITEM_RE.findall(summary) if i in WATCH}
            if not found:
                continue
            title = e.get("title") or ""
            m = NAME_RE.search(title)
            items.append(Item(
                source="edgar_8k", source_id=(e.get("id") or e.get("link"))[-60:], kind="filing",
                url=e.get("link"), title=clean_text(title, 300), body=summary,
                company_name=m.group(1) if m else None, published_at=updated,
                meta={"form": "8-K", "flags": sorted(found)},
            ))
        if stop:
            break
    db.kv_set("8k_since", newest.isoformat())
    return items, errors
