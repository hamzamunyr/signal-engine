"""Bulk backfill for high-volume, company-level intent signals.

Runs on GitHub Actions (internet). Everything lands in Supabase; Claude only reads summaries.
Sources:
  workable  - US companies with open outbound / lead-gen / sales-development roles (Workable public job search)
  reddit    - 90 days of posts in B2B communities that match buying-intent phrases (Arctic Shift search)
  hn        - 90 days of HN stories/comments with buying-intent phrases (Algolia)
Company names without a domain are resolved with Clearbit autocomplete (free, no key).
Each company gets ONE client_match row, so the LLM scores companies, not individual posts.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from . import db
from .http import http
from .item import Item
from .util import clean_text, company_domains_in_text, from_epoch, parse_date

log = logging.getLogger("bulk")
CLIENT = "pixelserve"

WORKABLE_QUERIES = [
    "sales development representative", "business development representative", "appointment setter",
    "lead generation specialist", "cold caller", "outbound sales", "business development manager",
    "inside sales representative", "demand generation", "growth marketing manager", "account executive",
    "sales manager", "head of sales", "client acquisition", "partnerships manager", "sales representative",
    "business development", "marketing manager", "sales associate", "new business", "sales consultant",
    "account manager", "marketing coordinator", "growth", "revenue operations", "sales director",
]
SUBREDDITS = [
    "sales", "B2BSales", "b2b_sales", "SaaS", "startups", "Entrepreneur", "smallbusiness", "agency", "marketing",
    "digital_marketing", "LeadGeneration", "coldemail", "consulting", "msp", "recruiting", "Accounting", "lawfirm",
    "EntrepreneurRideAlong", "GrowthHacking", "SaaSMarketing", "startup", "salesdevelopment", "sweatystartup",
    "Emailmarketing", "AskMarketing", "ITManagers", "managedservices", "Bookkeeping", "Insurance", "realtors",
    "CommercialRealEstate", "architecture", "engineering", "freelance", "webdev", "marketingagency",
]
INTENT_QUERIES = ["agency", "leads", "clients", "outbound", "SDR", "pipeline", "appointment", "outsource", "referrals",
                  "prospecting", "meetings"]
HN_QUERIES = ["lead generation agency", "outbound agency", "how do I get customers", "cold email", "first customers B2B",
              "hire SDR", "appointment setting", "sales agency"]
GTM_ROLE = re.compile(r"(sdr|bdr|sales|marketing|business development|appointment|lead gen|cold call|outbound|demand gen|"
                      r"growth|revenue|account (executive|manager)|client acquisition|partnership|new business)", re.I)
US_STATES = re.compile(r"\b(united states|usa|u\.s\.|remote - us|[A-Z]{2})\b")


def _workable(errors: list[str], max_pages: int = 60) -> list[Item]:
    by_co: dict[str, dict] = {}
    sample_saved = False
    for qy in WORKABLE_QUERIES:
        token = None
        for _ in range(max_pages):
            params = {"query": qy, "location": "United States"}
            if token:
                params["pageToken"] = token
            try:
                r = http.json("https://jobs.workable.com/api/v1/jobs", params=params, timeout=40, retries=1)
            except Exception as e:
                errors.append(f"workable '{qy}': {e}")
                break
            jobs = r.get("jobs") or r.get("results") or []
            if jobs and not sample_saved:
                db.kv_set("workable_sample", jobs[0])
                sample_saved = True
            for j in jobs:
                co = j.get("company") or {}
                name = (co.get("title") or co.get("name") or j.get("companyName") or "").strip()
                if not name:
                    continue
                title = j.get("title") or ""
                if not GTM_ROLE.search(title):
                    continue
                loc = j.get("location") or {}
                country = (loc.get("countryName") or loc.get("country") or "") if isinstance(loc, dict) else str(loc)
                if country and "united states" not in country.lower() and country.upper() not in ("US", "USA"):
                    continue
                key = (co.get("id") or name).__str__().lower()
                d = by_co.setdefault(key, {"name": name, "website": co.get("website") or co.get("url"),
                                           "roles": [], "desc": "", "url": j.get("url"), "loc": loc,
                                           "created": j.get("created") or j.get("published")})
                d["roles"].append(f"{title} ({(j.get('created') or j.get('published') or '')[:10]})")
                if not d["desc"]:
                    d["desc"] = clean_text(j.get("description") or j.get("descriptionHtml") or "", 1500)
            token = r.get("nextPageToken")
            if not token or not jobs:
                break
    items = []
    for key, d in by_co.items():
        loc = d["loc"] if isinstance(d["loc"], dict) else {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("subregion") or loc.get("region")] if x)
        roles = sorted(set(d["roles"]))
        body = (f"{d['name']} ({where or 'United States'}) currently has {len(roles)} open go-to-market role(s) on Workable: "
                f"{'; '.join(roles[:8])}. Excerpt from one job ad: {d['desc']}")
        items.append(Item(source="workable", source_id=f"co_{key}"[:200], kind="job", url=d["url"],
                          title=f"{d['name']} is hiring {len(roles)} outbound/sales role(s)", body=body,
                          company_name=d["name"], company_domain=_domain(d["website"]),
                          published_at=parse_date(d["created"]), meta={"roles": roles[:20], "location": where}))
    return items


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", url.lower())
    return m.group(1) if m else None


def _reddit(errors: list[str], days: int = 90) -> list[Item]:
    from .collectors.reddit import _post_item
    after = int(time.time()) - days * 86400
    out = []
    for sub in SUBREDDITS:
        for qy in INTENT_QUERIES:
            try:
                r = http.json("https://arctic-shift.photon-reddit.com/api/posts/search",
                              params={"subreddit": sub, "query": qy, "after": time.strftime("%Y-%m-%d", time.gmtime(after)), "limit": 100}, timeout=40, retries=0)
                out.extend(_post_item(d) for d in r.get("data") or [] if d.get("id"))
            except Exception as e:
                errors.append(f"reddit {sub}/{qy}: {str(e)[:80]}")
    return out


def _hn(errors: list[str], days: int = 90) -> list[Item]:
    from .collectors.hn import _hit_item
    since = int(time.time()) - days * 86400
    out = []
    for qy in HN_QUERIES:
        for page in range(3):
            try:
                r = http.json("https://hn.algolia.com/api/v1/search_by_date", params={
                    "query": qy, "tags": "(story,comment)", "hitsPerPage": 200, "page": page,
                    "numericFilters": f"created_at_i>{since}"})
                hits = r.get("hits", [])
                out.extend(_hit_item(h) for h in hits)
                if len(hits) < 200:
                    break
            except Exception as e:
                errors.append(f"hn {qy}: {e}")
                break
    return out


def _clearbit(name: str) -> str | None:
    try:
        r = http.json("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=15, retries=0)
        if r and isinstance(r, list):
            top = r[0]
            if re.sub(r"\W", "", top.get("name", "").lower())[:6] == re.sub(r"\W", "", name.lower())[:6]:
                return top.get("domain")
    except Exception:
        return None
    return None


def run(sources: list[str]) -> dict:
    errors: list[str] = []
    items: list[Item] = []
    t0 = time.time()
    if "workable" in sources:
        items += _workable(errors)
    if "reddit" in sources:
        items += _reddit(errors)
    if "hn" in sources:
        items += _hn(errors)
    # keep reddit/hn items that look like a buyer (cheap pre-filter; the LLM decides the rest)
    buyer = re.compile(r"(looking for|recommend|need (more|help)|how do (i|we) (get|find)|struggl|not working|dried up|"
                       r"hire|outsourc|agency|any advice|anyone (use|tried)|fired)", re.I)
    items = [i for i in items if i.source == "workable" or buyer.search(i.text())]
    resolved = 0
    for it in items:
        if it.company_name and not it.company_domain:
            it.company_domain = _clearbit(it.company_name)
            resolved += bool(it.company_domain)
    ids = db.insert_items(items)
    trig = {"workable": ("hiring", [32, 33, 34]), "reddit": ("pain", [1, 2, 3, 10]), "hn": ("pain", [1, 2, 3])}
    n = 0
    for iid in ids:
        src = db.q("select source from raw_items where id=%s", (iid,))[0]["source"]
        t, sig = trig.get(src, ("pain", [1]))
        db.x("""insert into client_matches (client,item_id,signal_ids,trigger,rule_score,lane,queue,status)
                values (%s,%s,%s,%s,%s,'B','new','new') on conflict do nothing""", (CLIENT, iid, sig, t, 5))
        n += 1
    by_src = defaultdict(int)
    for it in items:
        by_src[it.source] += 1
    res = {"found": dict(by_src), "new_items": len(ids), "matches": n, "clearbit_resolved": resolved,
           "errors": len(errors), "first_errors": errors[:5], "seconds": int(time.time() - t0)}
    db.kv_set("bulk_last", res)
    db.log_health("bulk", True, sum(by_src.values()), len(ids), "; ".join(errors[:3]), time.time() - t0)
    log.info("bulk: %s", res)
    return res
