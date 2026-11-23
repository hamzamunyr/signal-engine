"""Trustpilot 1-3 star reviews of each client's competitors (signals 25, 96-101).
Reads the public review pages (the JSON embedded in __NEXT_DATA__); G2/Capterra block scrapers."""
from __future__ import annotations

import json
import re

from ..config import union_list
from ..http import http
from ..item import Item
from ..util import clean_text, parse_date

NAME = "reviews"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def collect():
    errors: list[str] = []
    items: list[Item] = []
    for dom in union_list("competitor_domains"):
        url = f"https://www.trustpilot.com/review/{dom}?sort=recency&stars=1&stars=2&stars=3"
        try:
            r = http.get(url, browser=True, timeout=30, retries=1)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 403:  # Trustpilot blocks cloud servers; stop for this run
                errors.append("trustpilot blocked this server (403); churn signals come from Reddit/HN posts")
                break
            if code != 404:  # 404 = competitor has no Trustpilot page
                errors.append(f"{dom}: {e}")
            continue
        m = NEXT_RE.search(r.text)
        if not m:
            errors.append(f"{dom}: no __NEXT_DATA__")
            continue
        try:
            props = json.loads(m.group(1))["props"]["pageProps"]
        except Exception as e:
            errors.append(f"{dom}: parse {e}")
            continue
        biz = (props.get("businessUnit") or {}).get("displayName") or dom
        for rv in props.get("reviews") or []:
            rating = rv.get("rating") or 0
            flags = ["low_rating"] if rating <= 2 else []
            items.append(Item(
                source="trustpilot", source_id=str(rv.get("id")), kind="review",
                url=f"https://www.trustpilot.com/reviews/{rv.get('id')}",
                title=clean_text(f"[{biz}] {rv.get('title') or ''}", 300),
                body=clean_text(f"Review of {biz} ({rating} stars): {rv.get('text') or ''}", 3000),
                author=(rv.get("consumer") or {}).get("displayName"),
                published_at=parse_date((rv.get("dates") or {}).get("publishedDate")),
                meta={"competitor": biz, "competitor_domain": dom, "rating": rating, "flags": flags,
                      "reviewer_country": (rv.get("consumer") or {}).get("countryCode")},
            ))
    return items, errors
