"""Company domain resolution for leads that have only a company name (free, no keys).

1. Read the source article (direct feed links, not Google News redirects) and pick the outbound link
   whose domain matches the company name.
2. Otherwise try likely domains (name.com / .ai / .io / .co / .app / .tech) and keep one whose homepage
   title or body mentions the company name.
Results go to client_matches.company_domain with resolution_confidence 'medium'."""
from __future__ import annotations

import logging
import re

from . import db
from .http import http
from .util import GENERIC_DOMAINS, registrable_domain

log = logging.getLogger("resolve")

HREF_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
STOP = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "plc", "the", "labs", "lab",
        "technologies", "technology", "tech", "ai", "group", "holdings", "platform", "startup", "systems", "t", "a"}
NEWS_HOSTS = {"news.google.com", "eu-startups.com", "techcrunch.com", "crunchbase.com", "prnewswire.com",
              "businesswire.com", "globenewswire.com", "finsmes.com", "tech.eu", "sifted.eu", "yourstory.com",
              "inc42.com", "entrackr.com", "venturebeat.com", "forbes.com", "bloomberg.com", "reuters.com",
              "fortune.com", "axios.com", "wsj.com", "betakit.com", "siliconangle.com", "theinformation.com",
              "linkedin.com", "twitter.com", "x.com", "facebook.com", "youtube.com", "instagram.com", "medium.com",
              "wordpress.com", "wp.com", "gravatar.com", "googletagmanager.com", "cloudflare.com", "apple.com",
              "feedburner.com", "disqus.com", "addtoany.com", "sharethis.com", "producthunt.com"}


def name_tokens(name: str) -> list[str]:
    n = re.sub(r"\(.*?\)", " ", name.lower())
    toks = [t for t in re.findall(r"[a-z0-9]+", n) if t not in STOP and len(t) > 1]
    return toks


def _domain_matches(dom: str, toks: list[str]) -> bool:
    stem = dom.split(".")[0].replace("-", "")
    joined = "".join(toks)
    return bool(toks) and (stem == joined or stem.startswith(joined) or (len(toks[0]) >= 4 and stem.startswith(toks[0])))


def from_article(url: str, toks: list[str]) -> str | None:
    if not url or "news.google.com" in url:
        return None
    try:
        html = http.get(url, browser=True, timeout=20, retries=0).text
    except Exception:
        return None
    for href in HREF_RE.findall(html):
        d = registrable_domain(href)
        if not d or d in GENERIC_DOMAINS or d in NEWS_HOSTS or d == registrable_domain(url):
            continue
        if _domain_matches(d, toks):
            return d
    return None


def _site_mentions(dom: str, toks: list[str]) -> bool:
    try:
        r = http.get(f"https://{dom}", browser=True, timeout=12, retries=0)
    except Exception:
        return False
    text = r.text[:200_000].lower()
    m = TITLE_RE.search(text)
    title = m.group(1) if m else ""
    key = toks[0]
    return key in title or text.count(key) >= 3


def guess(toks: list[str]) -> str | None:
    if not toks:
        return None
    bases = list(dict.fromkeys(["".join(toks), "-".join(toks), toks[0]]))
    for base in bases:
        for tld in ("com", "ai", "io", "co", "app", "tech"):
            dom = f"{base}.{tld}"
            try:
                ans = http.json("https://dns.google/resolve", params={"name": dom, "type": "A"}, timeout=8, retries=0)
            except Exception:
                continue
            if ans.get("Status") == 0 and ans.get("Answer") and _site_mentions(dom, toks):
                return dom
    return None


def run(limit: int = 150) -> dict:
    rows = db.q("""
        select m.id, coalesce(m.company_name, r.company_name) as name, r.url
        from client_matches m join raw_items r on r.id = m.item_id
        where coalesce(m.company_domain, r.company_domain) is null
          and coalesce(m.company_name, r.company_name) is not null
          and m.queue in ('lane_a','auto_approved','human_review','new')
          and (m.queue <> 'new' or m.rule_score >= 7)
          and m.created_at > now() - interval '10 days'
        order by (m.queue='new'), m.rule_score desc limit %s""", (limit,))
    cache: dict[str, str | None] = {}
    found = 0
    for r in rows:
        toks = name_tokens(r["name"])
        key = " ".join(toks)
        if not key:
            continue
        if key not in cache:
            cache[key] = from_article(r["url"], toks) or guess(toks)
        dom = cache[key]
        if dom:
            db.x("update client_matches set company_domain=%s, resolution_confidence=coalesce(resolution_confidence,'medium') "
                 "where id=%s", (dom, r["id"]))
            found += 1
    log.info("resolved %d/%d (%d unique names)", found, len(rows), len(cache))
    return {"checked": len(rows), "resolved": found}
