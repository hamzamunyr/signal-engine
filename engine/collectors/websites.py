"""Weekly tech-stack and website change tracking (signals 62, 64, 69-83, 86).

Works on a list of company domains, not the whole internet: every company that already showed a
signal in the last 30 days, plus each client's `target_domains`. For each domain it saves a snapshot
(technologies from page scripts + DNS records, sitemap URLs, languages, blog cadence) and compares it
with last week's snapshot. The first snapshot of a domain is a baseline and emits nothing."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from psycopg.types.json import Jsonb

from .. import db
from ..config import union_list
from ..http import http
from ..item import Item
from ..util import now_utc, parse_date

NAME = "websites"
MAX_DOMAINS = 300

# name: (category, [html regexes], [dns regexes])
TECH = {
    "HubSpot": ("crm", [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"hs-analytics", r"_hsq"], [r"hubspot", r"hs-sites"]),
    "Salesforce": ("crm", [r"force\.com", r"salesforceliveagent"], [r"_spf\.salesforce\.com", r"salesforce"]),
    "Pipedrive": ("crm", [r"pipedriveassets", r"leadbooster"], [r"pipedrive"]),
    "Zoho CRM": ("crm", [r"salesiq\.zoho", r"zohopublic"], [r"zoho\.(com|eu)"]),
    "GoHighLevel": ("crm", [r"leadconnectorhq", r"msgsndr\.com"], [r"msgsndr", r"leadconnector"]),
    "Close": ("crm", [r"app\.close\.com"], [r"close\.io|close\.com"]),
    "Keap": ("crm", [r"infusionsoft", r"keap\.app"], [r"infusionmail"]),
    "Marketo": ("marketing_automation", [r"munchkin", r"mktoresp", r"marketo"], [r"mktoweb", r"marketo"]),
    "Pardot": ("marketing_automation", [r"pi\.pardot\.com", r"pardot"], [r"pardot"]),
    "Mailchimp": ("marketing_automation", [r"chimpstatic", r"list-manage\.com"], [r"servers\.mcsv\.net", r"mailchimp"]),
    "Klaviyo": ("marketing_automation", [r"klaviyo"], [r"klaviyo"]),
    "ActiveCampaign": ("marketing_automation", [r"trackcmp\.net", r"activehosted"], [r"activecampaign|acems"]),
    "Brevo": ("marketing_automation", [r"sibforms", r"sendinblue", r"brevo"], [r"sendinblue|brevo"]),
    "ConvertKit": ("marketing_automation", [r"convertkit", r"ck\.page"], [r"convertkit"]),
    "Customer.io": ("marketing_automation", [r"customer\.io"], [r"customeriomail|customer\.io"]),
    "Outreach": ("sales_engagement", [r"outreach\.io"], [r"outreach\.io|outrch"]),
    "Salesloft": ("sales_engagement", [r"salesloft"], [r"salesloft"]),
    "Apollo": ("sales_engagement", [r"assets\.apollo\.io", r"apollo\.io/micro"], [r"apollo\.io|apollomail"]),
    "lemlist": ("sales_engagement", [r"lemlist"], [r"lemlist"]),
    "Instantly": ("sales_engagement", [r"instantly\.ai"], [r"instantly"]),
    "Smartlead": ("sales_engagement", [r"smartlead"], [r"smartlead"]),
    "Reply.io": ("sales_engagement", [r"reply\.io"], [r"reply\.io"]),
    "RB2B": ("visitor_id", [r"rb2b"], []),
    "Warmly": ("visitor_id", [r"warmly\.ai"], []),
    "Koala": ("visitor_id", [r"getkoala\.com"], []),
    "Leadfeeder/Dealfront": ("visitor_id", [r"lftracker", r"leadfeeder", r"dealfront"], []),
    "Clearbit": ("visitor_id", [r"clearbit"], []),
    "ZoomInfo WebSights": ("visitor_id", [r"ws\.zoominfo\.com", r"zi-scripts"], []),
    "6sense": ("visitor_id", [r"6sc\.co", r"6sense"], []),
    "Intercom": ("ai_chat", [r"widget\.intercom\.io", r"intercomcdn"], []),
    "Drift": ("ai_chat", [r"js\.driftt\.com", r"drift\.com"], []),
    "Qualified": ("ai_chat", [r"qualified\.com"], []),
    "Tidio": ("ai_chat", [r"tidio"], []),
    "Crisp": ("ai_chat", [r"client\.crisp\.chat"], []),
    "Chatbase": ("ai_chat", [r"chatbase\.co"], []),
    "Voiceflow": ("ai_chat", [r"voiceflow"], []),
    "Zendesk": ("ai_chat", [r"zdassets\.com"], [r"zendesk"]),
    "Google Analytics 4": ("analytics", [r"gtag/js\?id=G-", r"G-[A-Z0-9]{8,}"], []),
    "Google Tag Manager": ("analytics", [r"googletagmanager\.com/gtm\.js"], []),
    "Segment": ("analytics", [r"cdn\.segment\.com"], []),
    "Mixpanel": ("analytics", [r"mixpanel"], []),
    "Amplitude": ("analytics", [r"amplitude\.com"], []),
    "Hotjar": ("analytics", [r"static\.hotjar\.com"], []),
    "Microsoft Clarity": ("analytics", [r"clarity\.ms"], []),
    "PostHog": ("analytics", [r"posthog"], []),
    "Heap": ("analytics", [r"heap-?analytics|heapanalytics"], []),
    "Plausible": ("analytics", [r"plausible\.io"], []),
    "Calendly": ("scheduling", [r"calendly\.com"], []),
    "Chili Piper": ("scheduling", [r"chilipiper"], []),
    "Cal.com": ("scheduling", [r"cal\.com/embed|app\.cal\.com"], []),
    "WordPress": ("cms", [r"wp-content", r"wp-includes"], []),
    "Webflow": ("cms", [r"webflow"], []),
    "Framer": ("cms", [r"framerusercontent", r"framer\.com"], []),
    "Wix": ("cms", [r"wixstatic", r"wix\.com"], []),
    "Squarespace": ("cms", [r"squarespace"], []),
    "Shopify": ("cms", [r"cdn\.shopify"], []),
    "Next.js": ("cms", [r"/_next/static"], []),
    "Google Workspace": ("email", [], [r"_spf\.google\.com|aspmx\.l\.google\.com"]),
    "Microsoft 365": ("email", [], [r"protection\.outlook\.com|spf\.protection\.outlook"]),
    "SendGrid": ("email", [], [r"sendgrid"]),
    "Mailgun": ("email", [], [r"mailgun"]),
}
COMPILED = {n: (c, [re.compile(p, re.I) for p in h], [re.compile(p, re.I) for p in d]) for n, (c, h, d) in TECH.items()}
SUBDOMAINS = ["go", "info", "email", "em", "links", "click", "track", "t", "pages", "lp", "marketing", "hello", "mail"]
LOCALE_RE = re.compile(r"^/([a-z]{2}(?:-[a-z]{2})?)(?:/|$)", re.I)
LOCALES = {"de", "fr", "es", "it", "nl", "pt", "pt-br", "ja", "ko", "zh", "en-gb", "en-us", "en-au", "en-ca", "sv",
           "da", "no", "fi", "pl", "tr", "ar", "he", "es-mx", "fr-ca", "de-de", "fr-fr", "uk", "au", "ca", "in"}
PAGE_KIND = [
    ("new_pricing_page", re.compile(r"/(pricing|plans|price)(/|$)", re.I)),
    ("new_product_page", re.compile(r"/(products?|platform|features?)/[^/]+", re.I)),
    ("new_service_page", re.compile(r"/(services?|solutions?|what-we-do|offerings?)/[^/]+", re.I)),
    ("new_landing_page", re.compile(r"/(lp|landing|go|get|demo|offer|webinar|campaigns?)/[^/]+", re.I)),
]
BLOG_RE = re.compile(r"/(blog|news|insights|articles|resources)/[^/]+", re.I)


def _dns(name: str, rtype: str) -> list[str]:
    try:
        r = http.json("https://dns.google/resolve", params={"name": name, "type": rtype}, timeout=10, retries=0)
        return [a.get("data", "") for a in r.get("Answer", []) or []]
    except Exception:
        return []


def _sitemap(domain: str) -> list[tuple[str, str | None]]:
    urls: list[tuple[str, str | None]] = []
    roots = [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"]
    try:
        rob = http.get(f"https://{domain}/robots.txt", browser=True, timeout=15, retries=0).text
        roots = re.findall(r"(?im)^sitemap:\s*(\S+)", rob)[:3] or roots
    except Exception:
        pass
    queue, seen = list(roots), set()
    while queue and len(seen) < 6 and len(urls) < 5000:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            xml = http.get(sm, browser=True, timeout=20, retries=0).text
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]+)\s*</lastmod>)?", xml, re.I)
        if "<sitemapindex" in xml.lower():
            queue.extend(u for u, _ in locs[:10])
        else:
            urls.extend(locs)
    return urls[:5000]


def snapshot(domain: str) -> dict:
    html = ""
    try:
        html = http.get(f"https://{domain}", browser=True, timeout=20, retries=0).text[:600_000]
    except Exception:
        try:
            html = http.get(f"https://www.{domain}", browser=True, timeout=20, retries=0).text[:600_000]
        except Exception:
            pass
    dns_blob = " ".join(_dns(domain, "TXT") + _dns(domain, "MX"))
    for sub in SUBDOMAINS:
        dns_blob += " " + " ".join(_dns(f"{sub}.{domain}", "CNAME"))
    tech = sorted(n for n, (c, hs, ds) in COMPILED.items()
                  if any(p.search(html) for p in hs) or any(p.search(dns_blob) for p in ds))
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html, re.I)
    sm = _sitemap(domain)
    paths = sorted({re.sub(r"^https?://[^/]+", "", u).rstrip("/") or "/" for u, _ in sm})
    locales = sorted({m.group(1).lower() for p in paths if (m := LOCALE_RE.match(p)) and m.group(1).lower() in LOCALES})
    blog = Counter()
    for u, lm in sm:
        if BLOG_RE.search(u) and lm:
            d = parse_date(lm)
            if d:
                blog[d.strftime("%Y-%m")] += 1
    return {"ok": bool(html or paths), "tech": tech, "generator": gen.group(1)[:80] if gen else None,
            "cms": [t for t in tech if TECH[t][0] == "cms"], "paths": paths[:4000], "locales": locales,
            "blog_by_month": dict(blog)}


def diff(prev: dict, cur: dict, watched: set[str]) -> tuple[set[str], list[str]]:
    flags: set[str] = set()
    notes: list[str] = []
    added = set(cur["tech"]) - set(prev.get("tech", []))
    removed = set(prev.get("tech", [])) - set(cur["tech"])
    cat = lambda names, c: {n for n in names if TECH.get(n, ("",))[0] == c}
    if added:
        notes.append("added: " + ", ".join(sorted(added)))
    if removed:
        notes.append("removed: " + ", ".join(sorted(removed)))
    if cat(added, "crm"):
        flags.add("tech_added_crm")
        if cat(removed, "crm"):
            flags.add("tech_switch_crm")
    if cat(added, "marketing_automation"):
        flags.add("tech_added_marketing_automation")
    if cat(added, "sales_engagement") | cat(added, "visitor_id"):
        flags.add("tech_added_sales_engagement")
        if cat(removed, "sales_engagement"):
            flags.add("tech_switch_sales")
    if cat(added, "ai_chat"):
        flags.add("tech_added_ai_chat")
    if cat(added, "analytics"):
        flags.add("tech_added_analytics")
    if {r.lower() for r in removed} & watched:
        flags.add("tech_removed_watched")
    if prev.get("cms") and cur["cms"] and set(prev["cms"]) != set(cur["cms"]):
        flags.add("redesign")
        notes.append(f"site platform {prev['cms']} -> {cur['cms']}")
    elif prev.get("generator") and cur.get("generator") and prev["generator"].split()[0] != cur["generator"].split()[0]:
        flags.add("redesign")
    new_paths = set(cur["paths"]) - set(prev.get("paths", []))
    if prev.get("paths"):
        for flag, rx in PAGE_KIND:
            hits = [p for p in new_paths if rx.search(p)]
            if hits:
                flags.add(flag)
                notes.append(f"{flag}: {', '.join(sorted(hits)[:5])}")
    new_loc = set(cur["locales"]) - set(prev.get("locales", []))
    if new_loc and prev.get("paths"):
        flags.add("new_locale")
        notes.append("new language/country sections: " + ", ".join(sorted(new_loc)))
    months = cur.get("blog_by_month", {})
    this = months.get(now_utc().strftime("%Y-%m"), 0)
    past = [months.get((now_utc() - timedelta(days=30 * i)).strftime("%Y-%m"), 0) for i in range(1, 4)]
    if this >= 4 and this >= 2 * max(1, sum(past) / 3):
        flags.add("blog_surge")
        notes.append(f"blog posts this month {this} vs avg {sum(past) / 3:.1f}")
    return flags, notes


def collect():
    errors: list[str] = []
    items: list[Item] = []
    watched = {t.lower() for t in union_list("tech_watch")}
    rows = db.q("""
        select domain, max(name) as name from (
          select coalesce(m.company_domain, r.company_domain) as domain, coalesce(m.company_name, r.company_name) as name,
                 coalesce(m.priority, m.rule_score, 0) as p
          from client_matches m join raw_items r on r.id=m.item_id
          where m.created_at > now() - interval '30 days' and m.queue <> 'rejected'
        ) t where domain is not null group by domain order by max(p) desc limit %s""", (MAX_DOMAINS,))
    domains = {r["domain"]: r["name"] for r in rows}
    for d in union_list("target_domains"):
        domains.setdefault(d, None)
    for dom, name in domains.items():
        try:
            cur = snapshot(dom)
        except Exception as e:
            errors.append(f"{dom}: {e}")
            continue
        if not cur["ok"]:
            continue
        prev = db.q("select data from site_snapshots where domain=%s and kind='site'", (dom,))
        db.x("insert into site_snapshots (domain, kind, taken_at, data) values (%s,'site',now(),%s) "
             "on conflict (domain, kind) do update set data=excluded.data, taken_at=now()", (dom, Jsonb(cur)))
        if not prev:
            continue
        flags, notes = diff(prev[0]["data"], cur, watched)
        if not flags:
            continue
        items.append(Item(
            source="websites", source_id=f"{dom}:{now_utc():%Y-%m-%d}", kind="website", url=f"https://{dom}",
            title=f"{name or dom}: website change", body="; ".join(notes)[:3000],
            company_name=name, company_domain=dom, published_at=now_utc(),
            meta={"flags": sorted(flags), "tech": cur["tech"]},
        ))
    return items, errors
