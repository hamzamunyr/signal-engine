"""Stack Exchange questions (signals 29, 93, 103) for clients that list sites + queries in
`stackexchange: {site: [queries]}`. Free without a key (small daily quota)."""
from __future__ import annotations

import time

from .. import db
from ..config import clients
from ..http import http
from ..item import Item
from ..util import clean_text, from_epoch

NAME = "stackexchange"


def collect():
    errors: list[str] = []
    items: list[Item] = []
    jobs: dict[str, set[str]] = {}
    for c in clients():
        for site, qs in (c.cfg.get("stackexchange") or {}).items():
            jobs.setdefault(site, set()).update(qs)
    if not jobs:
        return items, errors
    now = int(time.time())
    since = int(db.kv_get("se_since", now - 3 * 86400) or (now - 3 * 86400))
    for site, qs in jobs.items():
        for qy in qs:
            try:
                r = http.json("https://api.stackexchange.com/2.3/search/advanced", params={
                    "order": "desc", "sort": "creation", "q": qy, "site": site, "fromdate": since,
                    "filter": "withbody", "pagesize": 50})
            except Exception as e:
                errors.append(f"{site} '{qy}': {e}")
                continue
            for qd in r.get("items", []):
                items.append(Item(
                    source="stackexchange", source_id=f"{site}:{qd['question_id']}", kind="post",
                    url=qd.get("link"), title=clean_text(qd.get("title"), 300), body=clean_text(qd.get("body"), 4000),
                    author=(qd.get("owner") or {}).get("display_name"), published_at=from_epoch(qd.get("creation_date")),
                    meta={"site": site, "tags": qd.get("tags")},
                ))
            if r.get("backoff"):
                time.sleep(int(r["backoff"]) + 1)
    db.kv_set("se_since", now - 600)
    return items, errors
