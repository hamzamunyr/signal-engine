"""SEC Form D filings (private companies raising money). Free, needs a User-Agent with an email,
stays far under 10 requests/second. Feeds signals 42, 43, 46, 47, 106.

Form D lists the issuer, industry, amount raised, year of incorporation and the executives
(related persons) - so each item already carries decision-maker names."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import feedparser

from .. import db
from ..http import BOT_UA, http
from ..item import Item
from ..util import clean_text, now_utc, parse_date

NAME = "edgar_formd"
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&company=&dateb=&owner=include"
        "&start={start}&count=100&output=atom")
SKIP_INDUSTRIES = {
    "pooled investment fund", "reits and finance", "commercial", "residential", "other real estate",
    "oil and gas", "coal mining", "electric utilities", "investing", "lodging and conventions",
    "restaurants", "construction", "agriculture", "airlines and airports", "other energy",
}
VEHICLE_RE = re.compile(r"\b(spv|fund|funds|feeder|co-?invest|investors?|capital partners|holdings? lp|lp|l\.p\.|series \d+|trust)\b", re.I)
HEAD = {"User-Agent": BOT_UA, "Accept-Encoding": "gzip, deflate"}
LINK_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18})/")


def _txt(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def _parse_primary(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    for el in root.iter():  # strip namespaces
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    iss = root.find("primaryIssuer")
    off = root.find("offeringData")
    persons = []
    for p in root.findall("relatedPersonsList/relatedPersonInfo"):
        first = _txt(p, "relatedPersonName/firstName") or ""
        last = _txt(p, "relatedPersonName/lastName") or ""
        rels = [r.text for r in p.findall("relatedPersonRelationshipList/relationship") if r.text]
        persons.append({"name": f"{first} {last}".strip(), "roles": rels,
                        "clarification": _txt(p, "relationshipClarification")})
    amount = _txt(off, "offeringSalesAmounts/totalOfferingAmount") if off is not None else None
    sold = _txt(off, "offeringSalesAmounts/totalAmountSold") if off is not None else None
    return {
        "entity": _txt(iss, "entityName") if iss is not None else None,
        "state": _txt(iss, "issuerAddress/stateOrCountryDescription") if iss is not None else None,
        "city": _txt(iss, "issuerAddress/city") if iss is not None else None,
        "phone": _txt(iss, "issuerPhoneNumber") if iss is not None else None,
        "year_inc": _txt(iss, "yearOfInc/value") if iss is not None else None,
        "entity_type": _txt(iss, "entityType") if iss is not None else None,
        "industry": (_txt(off, "industryGroup/industryGroupType") or "") if off is not None else "",
        "revenue_range": _txt(off, "issuerSize/revenueRange") if off is not None else None,
        "amount": amount, "sold": sold,
        "securities": [t.tag for t in (off.find("typesOfSecuritiesOffered") if off is not None and off.find("typesOfSecuritiesOffered") is not None else [])],
        "first_sale": _txt(off, "dateOfFirstSale/value") if off is not None else None,
        "persons": persons[:8],
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect():
    errors: list[str] = []
    items: list[Item] = []
    since = parse_date(db.kv_get("formd_since")) or (now_utc() - timedelta(days=2))
    newest = since
    seen = 0
    for start in range(0, 600, 100):
        try:
            r = http.get(FEED.format(start=start), headers=HEAD, timeout=40)
            entries = feedparser.parse(r.content).entries
        except Exception as e:
            errors.append(f"feed start={start}: {e}")
            break
        if not entries:
            break
        stop = False
        for e in entries:
            title = e.get("title") or ""
            if not title.startswith("D -"):
                continue  # skip D/A amendments
            updated = parse_date(e.get("updated"))
            if updated and updated <= since:
                stop = True
                continue
            newest = max(newest, updated or newest)
            m = LINK_RE.search(e.get("link") or "")
            if not m:
                continue
            cik, acc = m.group(1), m.group(2)
            seen += 1
            try:
                x = http.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml", headers=HEAD, timeout=30)
                d = _parse_primary(x.content)
            except Exception as ex:
                errors.append(f"primary_doc {cik}/{acc}: {ex}")
                continue
            if VEHICLE_RE.search(d.get("entity") or ""):
                continue  # investment vehicles, not operating companies
            if d["industry"].lower() in SKIP_INDUSTRIES or (d.get("entity_type") or "").lower().startswith("limited partnership"):
                continue
            amount = _num(d["amount"]) or _num(d["sold"])
            flags = ["form_d"]
            year = int(d["year_inc"]) if (d.get("year_inc") or "").isdigit() else None
            if (amount is not None and amount <= 5_000_000) or (year and year >= now_utc().year - 2):
                flags.append("form_d_small")
            if year and year >= now_utc().year - 1:
                flags.append("new_company_listing")
            people = ", ".join(f"{p['name']} ({'/'.join(p['roles'])})" for p in d["persons"])
            body = clean_text(
                f"{d['entity']} filed Form D: raising {d['amount'] or 'n/a'} (sold {d['sold'] or 'n/a'}), "
                f"industry {d['industry']}, revenue range {d['revenue_range']}, incorporated {d['year_inc']}, "
                f"{d['city']}, {d['state']}. People: {people}", 3000)
            items.append(Item(
                source="edgar_formd", source_id=acc, kind="filing",
                url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/",
                title=f"Form D: {d['entity']} raising {d['amount'] or 'undisclosed'}", body=body,
                company_name=d["entity"], published_at=updated,
                meta={"form": "D", "flags": flags, **{k: d[k] for k in ("industry", "amount", "sold", "year_inc",
                      "state", "city", "phone", "revenue_range", "persons", "first_sale")}},
            ))
        if stop:
            break
    db.kv_set("formd_since", newest.isoformat())
    if seen == 0 and not errors:
        pass
    return items, errors
