"""Matches newly collected items to each client's signals (rule pass), scores them and picks Lane A.

Lane A = same-day queue, no LLM wait: strong explicit intent or churn posted in the last 48 hours,
or fresh funding. Everything matched also goes to the daily LLM batch (Lane B)."""
from __future__ import annotations

import logging
import math
import re

from . import db
from .config import Client, clients
from .util import now_utc

log = logging.getLogger("match")

LANE_A_GROUPS = {"explicit_intent", "churn", "first_party"}
SENSITIVE_KINDS = {"post", "comment"}
BOT_AUTHORS = {"automoderator", "[deleted]", "sneakpeekbot", "remindmebot", "savevideo", "visualmod"}
BOT_TEXT = ("i am a bot", "you must first be an active community member", "this action was performed automatically")


AMOUNT_RE = re.compile(r"([\d.,]+)\s*(k|thousand|m|million|mn|b|billion)?", re.I)


def amount_usd(meta: dict) -> float | None:
    """Rough amount from news meta ('$4.5M', '£1.8 million') or Form D (dollars)."""
    raw = meta.get("amount")
    if raw in (None, ""):
        return None
    m = AMOUNT_RE.search(str(raw).replace(",", ""))
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    return v * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mn": 1e6, "b": 1e9, "billion": 1e9}.get(unit, 1)


def _age_days(published, first_seen) -> float:
    ref = published or first_seen
    if not ref:
        return 0.0
    return max(0.0, (now_utc() - ref).total_seconds() / 86400)


def match_row(c: Client, row: dict) -> dict | None:
    kind, source = row["kind"], row["source"]
    if c.skips(source):
        return None
    meta = row.get("meta") or {}
    title = row.get("title") or ""
    text = f"{title}\n{row.get('body') or ''}"
    if kind == "review" and meta.get("competitor"):
        text = f"{meta['competitor']} {text}"
    if kind in SENSITIVE_KINDS:
        if (row.get("author") or "").lower() in BOT_AUTHORS or any(b in text.lower() for b in BOT_TEXT):
            return None
        if c.negative_re is not None and c.negative_re.search(text):
            return None
    flags = set(meta.get("flags") or [])
    max_emp = (c.cfg.get("icp") or {}).get("max_employees")
    if kind == "job" and max_emp and (meta.get("open_roles") or 0) > max(15, int(max_emp) * 0.5):
        return None   # too many open roles for a small-company ICP
    hits = [r for r in c.rules.values() if r.matches(kind, text, title, flags)]
    if not hits:
        return None
    # Outside the client's communities, keep only explicit intent / churn on Reddit
    if source == "reddit" and c.communities and (meta.get("subreddit") or "") not in c.communities:
        hits = [r for r in hits if r.group in LANE_A_GROUPS]
        if not hits:
            return None
    age = _age_days(row.get("published_at"), row.get("first_seen_at"))
    top = max(hits, key=lambda r: r.weight)
    if age > top.stop_after:
        return None
    decay = math.pow(2, -age / top.half_life)
    raw = min(10.0, top.weight + 0.5 * (len(hits) - 1))
    score = round(raw * decay, 2)
    lane = "B"
    if top.group in LANE_A_GROUPS and age <= 2 and raw >= 9 and kind != "comment":
        lane = "A"
    elif top.group == "funding" and age <= 2 and kind in ("news", "filing") and (
            "form_d_small" in flags or any(h.id in (42, 43) for h in hits)) and (amount_usd(meta) or 0) <= 15e6:
        lane = "A"
    return {"signal_ids": sorted(h.id for h in hits), "trigger": top.group, "rule_score": score, "lane": lane}


def run(limit: int = 50000, rebuild: bool = False) -> dict:
    if rebuild:  # re-match everything not yet scored or exported (after changing rules or client configs)
        db.x("delete from client_matches where exported_at is null and llm_scored_at is null")
        db.kv_set("match_last_id", 0)
    last_id = int(db.kv_get("match_last_id", 0) or 0)
    rows = db.q("select id, source, kind, title, body, author, meta, published_at, first_seen_at, company_name, company_domain "
                "from raw_items where id > %s order by id limit %s", (last_id, limit))
    if not rows:
        return {"items": 0, "matches": 0}
    stats = {"items": len(rows), "matches": 0, "lane_a": 0}
    cl = clients()
    for c in cl:
        db.x("insert into clients (name, config_version, active) values (%s,%s,true) "
             "on conflict (name) do update set active=true, updated_at=now()", (c.name, str(hash(str(c.cfg)))[:12]))
    with db.tx() as conn, conn.cursor() as cur:
        for row in rows:
            for c in cl:
                m = match_row(c, row)
                if not m:
                    continue
                cur.execute(
                    "insert into client_matches (client, item_id, signal_ids, trigger, rule_score, lane, company_name, "
                    "company_domain, queue) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (client, item_id) do nothing",
                    (c.name, row["id"], m["signal_ids"], m["trigger"], m["rule_score"], m["lane"],
                     row.get("company_name"), row.get("company_domain"), "lane_a" if m["lane"] == "A" else "new"))
                stats["matches"] += cur.rowcount
                stats["lane_a"] += cur.rowcount if m["lane"] == "A" else 0
    db.kv_set("match_last_id", rows[-1]["id"])
    log.info("match: %s", stats)
    return stats
