"""Sends review-ready leads to each client's Google Sheet (through a small Apps Script web app),
merging all evidence for one company into one row (one company, one timeline)."""
from __future__ import annotations

import logging
import os
import re

from psycopg.types.json import Jsonb

from . import db
from .config import catalog, clients
from .http import http
from .llm import kaggle_ready

log = logging.getLogger("export")

QUEUE_RANK = {"lane_a": 0, "auto_approved": 1, "human_review": 2, "unscored": 3}


def company_key(row: dict) -> str:
    if row.get("company_domain"):
        return row["company_domain"].lower()
    if row.get("company_name"):
        n = re.sub(r"[^a-z0-9]+", " ", row["company_name"].lower())
        n = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc)\b", "", n)
        n = " ".join(n.split())
        if n:
            return "name:" + n
    return "url:" + (row.get("url") or str(row["mid"]))


def _webhook(client: str) -> tuple[str | None, str | None]:
    url = os.environ.get(f"SHEET_WEBHOOK_{client.upper().replace('-', '_')}") or os.environ.get("SHEET_WEBHOOK")
    return url, os.environ.get("SHEET_TOKEN")


def candidates(client: str, limit: int, include_unscored: bool) -> list[dict]:
    queues = ["lane_a", "auto_approved", "human_review"] + (["new"] if include_unscored else [])
    rows = db.q("""
        select m.id as mid, m.*, r.source, r.kind, r.url, r.title, r.body, r.author, r.published_at, r.first_seen_at,
               r.meta, coalesce(m.company_name, r.company_name) as company_name,
               coalesce(m.company_domain, r.company_domain) as company_domain
        from client_matches m join raw_items r on r.id = m.item_id
        where m.client=%s and m.exported_at is null and m.queue = any(%s)
          and (m.queue <> 'new' or (m.rule_score >= 8 and m.created_at > now() - interval '2 days'))
        order by m.priority desc nulls last, m.rule_score desc limit %s""", (client, queues, limit * 4))
    for r in rows:
        if r["queue"] == "new":
            r["queue"] = "unscored"
    rows.sort(key=lambda r: (QUEUE_RANK.get(r["queue"], 9), -(r["priority"] or r["rule_score"] or 0)))
    return rows


def to_sheet_row(r: dict) -> dict:
    cat = catalog()["signals"]
    meta = r.get("meta") or {}
    sigs = ", ".join(f"{i} {cat[i]['name']}" for i in (r["signal_ids"] or []) if i in cat)
    contact = ""
    if meta.get("persons"):
        contact = "; ".join(f"{p.get('name')} ({'/'.join(p.get('roles') or [])})" for p in meta["persons"][:4])
    elif r.get("author") and r["kind"] in ("post", "comment"):
        contact = f"{r['source']} user: {r['author']}"
    date = (r.get("published_at") or r.get("first_seen_at"))
    quote = r.get("evidence_quote") or (r.get("title") or "")[:200] or (r.get("body") or "")[:200]
    return {
        "key": company_key(r),
        "company": r.get("company_name") or "",
        "domain": r.get("company_domain") or "",
        "priority": round(float(r.get("priority") or r.get("rule_score") or 0), 1),
        "queue": r["queue"],
        "trigger": r.get("trigger") or "",
        "signals": sigs,
        "intent": r.get("intent_score") if r.get("intent_score") is not None else "",
        "icp_fit": r.get("icp_fit_score") if r.get("icp_fit_score") is not None else "",
        "size": r.get("employee_estimate") or "",
        "evidence": quote,
        "angle": r.get("recommended_angle") or "",
        "contact": contact or (r.get("decision_maker_role") or ""),
        "dm_role": r.get("decision_maker_role") or "",
        "source": f"{r['source']}" + (f" r/{meta['subreddit']}" if meta.get("subreddit") else ""),
        "link": r.get("url") or "",
        "signal_date": date.strftime("%Y-%m-%d %H:%M") if date else "",
        "timeline_entry": f"{date:%b %d}: {sigs.split(',')[0] if sigs else r.get('trigger')} ({r['source']})" if date else "",
    }


def _update_company(row: dict, r: dict):
    entry = {"date": row["signal_date"], "signals": r["signal_ids"], "source": r["source"], "url": row["link"],
             "trigger": row["trigger"], "priority": row["priority"]}
    db.x("""insert into companies (key, name, domain, resolution_confidence, employee_estimate, last_signal_at, timeline)
            values (%s,%s,%s,%s,%s,now(),%s)
            on conflict (key) do update set name=coalesce(companies.name, excluded.name),
              domain=coalesce(companies.domain, excluded.domain), last_signal_at=now(),
              employee_estimate=coalesce(excluded.employee_estimate, companies.employee_estimate),
              timeline=companies.timeline || excluded.timeline""",
         (row["key"], row["company"] or None, row["domain"] or None, r.get("resolution_confidence"),
          r.get("employee_estimate"), Jsonb([entry])))


def run(include_unscored: bool | None = None) -> dict:
    if include_unscored is None:
        include_unscored = not kaggle_ready()
    out = {}
    for c in clients():
        if not (c.cfg.get("output") or {}).get("sheet", True):
            continue
        url, token = _webhook(c.name)
        daily = int(c.cfg.get("leads_per_day", 30))
        sent_today = db.q("select count(*) as n from client_matches where client=%s and exported_at > now() - interval '24 hours'",
                          (c.name,))[0]["n"]
        room = max(0, daily - sent_today)
        cands = candidates(c.name, daily, include_unscored)
        lane_a = [r for r in cands if r["queue"] == "lane_a"]
        rest = [r for r in cands if r["queue"] != "lane_a"][:room]
        rows = lane_a + rest   # Lane A is never held back by the daily cap
        if not rows:
            out[c.name] = 0
            continue
        payload = [to_sheet_row(r) for r in rows]
        if not url:
            log.warning("no SHEET_WEBHOOK for %s; %d rows ready but not sent", c.name, len(rows))
            out[c.name] = f"{len(rows)} ready, no webhook"
            continue
        resp = http.post(url, json={"token": token, "client": c.name, "rows": payload}, timeout=60, retries=1)
        try:
            body = resp.json()
        except ValueError:
            body = {"ok": False, "raw": resp.text[:300]}
        if not body.get("ok"):
            raise RuntimeError(f"sheet webhook rejected rows: {body}")
        for row, r in zip(payload, rows):
            _update_company(row, r)
        db.x("update client_matches set exported_at=now(), status='sent_to_sheet', "
             "queue = case when queue='new' then 'unscored' else queue end where id = any(%s)", ([r["mid"] for r in rows],))
        out[c.name] = body
        log.info("%s: sent %d rows -> %s", c.name, len(rows), body)
    return out
