"""Finds company job-board slugs (Greenhouse, Lever, Ashby, Workable) to watch daily.

1. SearXNG (self-hosted in the weekly GitHub Action) searches like site:jobs.lever.co "SDR".
2. Companies that already showed another signal (funding, launches, posts with a domain) get their
   likely slugs probed directly on each board API, so funded/launching companies get hiring checks too.
Returns no items; it only fills the job_boards table."""
from __future__ import annotations

import logging
import os
import re

from .. import db
from ..http import http

NAME = "job_discovery"
log = logging.getLogger(NAME)

BOARD_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][a-z0-9_-]{1,60})", re.I),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9][a-z0-9_.-]{1,60})", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_.%-]{1,60})", re.I),
    "workable": re.compile(r"apply\.workable\.com/([a-z0-9][a-z0-9_-]{1,60})", re.I),
}
SITES = {"greenhouse": "boards.greenhouse.io", "lever": "jobs.lever.co", "ashby": "jobs.ashbyhq.com",
         "workable": "apply.workable.com"}
ROLE_QUERIES = ['"SDR"', '"Sales Development Representative"', '"BDR"', '"Business Development Representative"',
                '"Founding Account Executive"', '"Head of Sales"', '"First Sales"', '"Account Executive" startup',
                '"Growth Marketing Manager"', '"Demand Generation"', '"Appointment Setter"', '"Head of Growth"']
BAD_SLUGS = {"embed", "jobs", "job_board", "api", "v1", "search", "careers", "apply"}


def _add(board: str, slug: str, name: str | None = None, domain: str | None = None) -> bool:
    slug = slug.strip("/").lower()
    if not slug or slug in BAD_SLUGS:
        return False
    n = db.x("insert into job_boards (board, slug, company_name, domain) values (%s,%s,%s,%s) "
             "on conflict (board, slug) do nothing", (board, slug, name, domain))
    if not n and domain:
        db.x("update job_boards set domain=%s where board=%s and slug=%s and domain is null", (domain, board, slug))
    return n > 0


def _searx(errors: list[str]) -> int:
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return 0
    added = 0
    for board, site in SITES.items():
        for role in ROLE_QUERIES:
            for page in (1, 2, 3):
                try:
                    r = http.json(f"{base}/search", params={"q": f"site:{site} {role}", "format": "json",
                                                            "pageno": page, "time_range": "month"}, timeout=40, retries=0)
                except Exception as e:
                    errors.append(f"searx {board} {role}: {e}")
                    break
                results = r.get("results", [])
                for res in results:
                    m = BOARD_PATTERNS[board].search(res.get("url", ""))
                    if m:
                        added += _add(board, m.group(1))
                if not results:
                    break
    return added


def _probe(slug: str) -> list[str]:
    hits = []
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
    }
    for board, url in urls.items():
        try:
            r = http.get(url, timeout=15, retries=0)
            if r.status_code == 200 and r.text.strip() not in ("[]", ""):
                hits.append(board)
        except Exception:
            pass
    return hits


def _from_signals(errors: list[str]) -> int:
    rows = db.q("""
        select distinct coalesce(m.company_domain, r.company_domain) as domain,
                        coalesce(m.company_name, r.company_name) as name
        from client_matches m join raw_items r on r.id = m.item_id
        where m.created_at > now() - interval '10 days'
          and r.kind in ('news','filing','launch','post')
          and coalesce(m.company_domain, r.company_domain, m.company_name, r.company_name) is not null
        limit 400""")
    probed = set(db.kv_get("probed_slugs", []) or [])
    added = 0
    for row in rows:
        cands = []
        if row["domain"]:
            cands.append(row["domain"].split(".")[0])
        if row["name"]:
            n = re.sub(r"[^a-z0-9 ]", "", row["name"].lower())
            n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|labs|hq)\b", "", n).strip()
            if n:
                cands += [n.replace(" ", ""), n.replace(" ", "-")]
        for slug in dict.fromkeys(cands):
            if not slug or slug in probed or len(slug) < 3:
                continue
            probed.add(slug)
            for board in _probe(slug):
                added += _add(board, slug, row["name"], row["domain"])
    db.kv_set("probed_slugs", sorted(probed)[-20000:])
    return added


def collect():
    errors: list[str] = []
    a = _searx(errors)
    b = _from_signals(errors)
    # Boards linked from collected items (HN Who is hiring, posts)
    c = 0
    for row in db.q("select body from raw_items where first_seen_at > now() - interval '8 days' "
                    "and (body ilike '%%greenhouse.io%%' or body ilike '%%lever.co%%' or body ilike '%%ashbyhq.com%%' "
                    "or body ilike '%%workable.com%%') limit 3000"):
        for board, pat in BOARD_PATTERNS.items():
            for m in pat.finditer(row["body"] or ""):
                c += _add(board, m.group(1))
    total = db.q("select count(*) as n from job_boards where active")[0]["n"]
    log.info("job discovery: searx=%d probed=%d linked=%d total_active=%d", a, b, c, total)
    if not (a or b or c):
        errors.append(f"no new boards this run (searx={a} probed={b} linked={c} total_active={total})")
    return [], errors
