"""Job boards with free public APIs: Greenhouse, Lever, Ashby, Workable.

Checks every known company board (table job_boards) daily, emits sales / growth roles posted in the
last 30 days, and sets company-level flags by comparing with the last check:
  multi_sales_roles (40), first_sales_leader (41), many_open_roles (66), sales_roles_growing (67),
  first_sales_hire (108), new_job_country (64, 109)."""
from __future__ import annotations

import re
from datetime import timedelta

from .. import db
from ..config import _compile_phrase, _union, catalog
from ..http import http
from ..item import Item
from ..util import clean_text, from_epoch, now_utc, parse_date

NAME = "jobs"
MAX_BOARDS_PER_RUN = 2500
RECENT_DAYS = 30

SALES_RE = _union([_compile_phrase(p) for p in catalog()["vocab"]["sales_titles"]])
LEADER_RE = re.compile(r"head of sales|vp,? (of )?sales|vice president,? sales|vice president of sales|"
                       r"sales director|director of sales|chief revenue|\bcro\b|head of growth|"
                       r"founding (account executive|sales)|first sales|sales lead", re.I)
SKIP_RE = re.compile(r"\b(engineer|developer|scientist|nurse|driver|technician|intern)\b", re.I)


def _country(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = [p.strip() for p in re.split(r"[,/|–-]", loc) if p.strip()]
    return parts[-1][:40] if parts else None


def fetch_board(board: str, slug: str) -> tuple[str | None, list[dict]]:
    """Returns (company_name, jobs[{id,title,url,location,posted,text,dept}])."""
    if board == "greenhouse":
        meta = http.json(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=20, retries=1)
        data = http.json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}, timeout=30, retries=1)
        return meta.get("name"), [{
            "id": j["id"], "title": j.get("title"), "url": j.get("absolute_url"),
            "location": (j.get("location") or {}).get("name"),
            "posted": parse_date(j.get("first_published") or j.get("updated_at")),
            "text": clean_text(j.get("content"), 3000),
            "dept": ", ".join(d.get("name", "") for d in j.get("departments") or []),
        } for j in data.get("jobs", [])]
    if board == "lever":
        data = http.json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=30, retries=1)
        return None, [{
            "id": j["id"], "title": j.get("text"), "url": j.get("hostedUrl"),
            "location": (j.get("categories") or {}).get("location"),
            "posted": from_epoch((j.get("createdAt") or 0) / 1000),
            "text": clean_text(j.get("descriptionPlain") or j.get("description"), 3000),
            "dept": (j.get("categories") or {}).get("team"),
        } for j in data or []]
    if board == "ashby":
        data = http.json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30, retries=1)
        return None, [{
            "id": j["id"], "title": j.get("title"), "url": j.get("jobUrl"), "location": j.get("location"),
            "posted": parse_date(j.get("publishedAt")), "text": clean_text(j.get("descriptionPlain"), 3000),
            "dept": j.get("department"),
        } for j in data.get("jobs", [])]
    if board == "workable":
        data = http.json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=30, retries=1)
        return data.get("name"), [{
            "id": j.get("shortcode"), "title": j.get("title"), "url": j.get("url"),
            "location": ", ".join(x for x in [j.get("city"), j.get("country")] if x),
            "posted": parse_date(j.get("published_on") or j.get("created_at")), "text": "",
            "dept": j.get("department"),
        } for j in data.get("jobs", [])]
    raise ValueError(board)


def _is_sales(title: str) -> bool:
    return bool(title and SALES_RE and SALES_RE.search(title) and not SKIP_RE.search(title))


def process_board(board: str, slug: str, known_name: str | None, domain: str | None) -> tuple[list[Item], str | None]:
    name, jobs = fetch_board(board, slug)
    name = name or known_name or slug.replace("-", " ").title()
    hist_key = f"jobhist:{board}:{slug}"
    prev = db.kv_get(hist_key)
    sales = [j for j in jobs if _is_sales(j["title"] or "")]
    countries = sorted({c for c in (_country(j["location"]) for j in jobs) if c})
    total, n_sales = len(jobs), len(sales)
    flags: set[str] = set()
    if n_sales >= 3:
        flags.add("multi_sales_roles")
    leaders = [j for j in sales if LEADER_RE.search(j["title"] or "")]
    if leaders and n_sales == len(leaders):
        flags.add("first_sales_leader")
    if prev:
        if total >= 5 and total > prev.get("total", 0):
            flags.add("many_open_roles")
        if n_sales > prev.get("sales", 0) and prev.get("sales", 0) > 0:
            flags.add("sales_roles_growing")
        if n_sales >= 1 and not prev.get("seen_sales"):
            flags.add("first_sales_hire")
        new_c = set(countries) - set(prev.get("countries", []))
        if new_c and prev.get("countries"):
            flags.add("new_job_country")
    elif n_sales == 1 and total <= 15:
        flags.add("first_sales_hire")
    db.kv_set(hist_key, {"total": total, "sales": n_sales, "countries": countries,
                         "seen_sales": bool((prev or {}).get("seen_sales")) or n_sales > 0,
                         "checked": now_utc().isoformat()})
    cutoff = now_utc() - timedelta(days=RECENT_DAYS)
    out: list[Item] = []
    for j in sales:
        if j["posted"] and j["posted"] < cutoff:
            continue
        out.append(Item(
            source=f"jobs_{board}", source_id=f"{slug}:{j['id']}", kind="job", url=j["url"],
            title=j["title"], body=clean_text(f"{j['title']} at {name}. {j['location'] or ''}. {j['dept'] or ''}. {j['text']}", 3500),
            company_name=name, company_domain=domain, published_at=j["posted"],
            meta={"board": board, "slug": slug, "location": j["location"], "flags": sorted(flags),
                  "open_roles": total, "sales_roles": n_sales, "sales_titles": [s["title"] for s in sales][:10]},
        ))
    if not out and flags & {"many_open_roles", "new_job_country"}:
        out.append(Item(
            source=f"jobs_{board}", source_id=f"{slug}:summary:{now_utc():%Y-%m-%d}", kind="job",
            url=jobs[0]["url"] if jobs else None, title=f"{name}: {total} open roles",
            body=f"{name} has {total} open roles ({n_sales} in sales). Locations: {', '.join(countries)}",
            company_name=name, company_domain=domain, published_at=now_utc(),
            meta={"board": board, "slug": slug, "flags": sorted(flags), "open_roles": total, "sales_roles": n_sales},
        ))
    return out, name


def collect():
    errors: list[str] = []
    items: list[Item] = []
    boards = db.q("select board, slug, company_name, domain from job_boards where active "
                  "order by last_checked_at nulls first limit %s", (MAX_BOARDS_PER_RUN,))
    for b in boards:
        try:
            got, name = process_board(b["board"], b["slug"], b["company_name"], b["domain"])
            items.extend(got)
            db.x("update job_boards set last_checked_at=now(), company_name=coalesce(company_name,%s) "
                 "where board=%s and slug=%s", (name, b["board"], b["slug"]))
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 404:
                db.x("update job_boards set active=false where board=%s and slug=%s", (b["board"], b["slug"]))
            else:
                errors.append(f"{b['board']}/{b['slug']}: {e}")
    if not boards:
        errors.append("no job boards yet: run job_discovery")
    return items, errors
