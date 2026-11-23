"""Public tenders (signals 39, 87, 88, 91): EU TED (anonymous search API) and UK Find a Tender (OCDS).
Runs only for clients with `tender_keywords`."""
from __future__ import annotations

from datetime import timedelta

from ..config import clients, union_list
from ..http import http
from ..item import Item
from ..util import clean_text, now_utc, parse_date, phrase_regex

NAME = "tenders"


def _ted(keywords: list[str], errors: list[str]) -> list[Item]:
    out: list[Item] = []
    since = (now_utc() - timedelta(days=3)).strftime("%Y%m%d")
    for kw in keywords:
        body = {"query": f'FT~"{kw}" AND PD>={since}', "fields": [
            "publication-number", "notice-title", "buyer-name", "notice-type", "publication-date",
            "buyer-country", "deadline-receipt-tender-date-lot"], "limit": 50, "page": 1,
            "paginationMode": "PAGE_NUMBER", "scope": "ACTIVE"}
        try:
            r = http.post("https://api.ted.europa.eu/v3/notices/search", json=body, timeout=40).json()
        except Exception as e:
            errors.append(f"ted '{kw}': {e}")
            continue
        for n in r.get("notices", []):
            pub = n.get("publication-number")
            title = n.get("notice-title") or {}
            title = title.get("eng") or next(iter(title.values()), "") if isinstance(title, dict) else str(title)
            buyer = n.get("buyer-name") or {}
            buyer = (buyer.get("eng") or next(iter(buyer.values()), [""])) if isinstance(buyer, dict) else buyer
            buyer = buyer[0] if isinstance(buyer, list) and buyer else buyer
            ntype = n.get("notice-type")
            flags = ["tender"] + (["tender_rfi"] if ntype and "pin" in str(ntype).lower() else [])
            out.append(Item(
                source="ted", source_id=str(pub), kind="tender",
                url=f"https://ted.europa.eu/en/notice/-/detail/{pub}", title=clean_text(str(title), 400),
                body=clean_text(f"{title}. Buyer: {buyer}. Type: {ntype}. Keyword: {kw}", 1500),
                company_name=str(buyer) if buyer else None, published_at=parse_date(n.get("publication-date")),
                meta={"flags": flags, "notice_type": ntype, "country": n.get("buyer-country"), "keyword": kw},
            ))
    return out


def _find_a_tender(keywords: list[str], errors: list[str]) -> list[Item]:
    out: list[Item] = []
    rx = phrase_regex(keywords)
    since = (now_utc() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    params = {"updatedFrom": since, "limit": 100}
    for _ in range(15):
        try:
            r = http.json(url, params=params, timeout=40)
        except Exception as e:
            errors.append(f"find_a_tender: {e}")
            break
        for rel in r.get("releases", []):
            t = rel.get("tender") or {}
            text = f"{t.get('title') or ''} {t.get('description') or ''}"
            if not rx or not rx.search(text):
                continue
            buyer = (rel.get("buyer") or {}).get("name")
            tags = rel.get("tag") or []
            flags = ["tender"] + (["tender_rfi"] if "planning" in tags else [])
            out.append(Item(
                source="find_a_tender", source_id=rel.get("ocid") or rel.get("id"), kind="tender",
                url=f"https://www.find-tender.service.gov.uk/Notice/{(rel.get('id') or '').split('-')[-1]}",
                title=clean_text(t.get("title"), 400), body=clean_text(f"{text}. Buyer: {buyer}", 2500),
                company_name=buyer, published_at=parse_date(rel.get("date")), meta={"flags": flags, "tags": tags},
            ))
        nxt = (r.get("links") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, None
    return out


def collect():
    errors: list[str] = []
    kws = union_list("tender_keywords")
    if not kws:
        return [], []
    items: list[Item] = []
    if any(not c.skips("ted") and c.cfg.get("tender_keywords") for c in clients()):
        items += _ted(kws, errors)
    if any(not c.skips("find_a_tender") and c.cfg.get("tender_keywords") for c in clients()):
        items += _find_a_tender(kws, errors)
    return items, errors
