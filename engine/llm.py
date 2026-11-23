"""Lane B: daily LLM scoring on a free Kaggle GPU.

score-push: picks unscored matches, writes prompts into the Kaggle script (no database access or
            secrets needed on Kaggle), and pushes it with `kaggle kernels push`.
score-pull: waits for the run, downloads results.jsonl, parses the JSON and sets priority + queue.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import db
from .config import ROOT, catalog, clients
from .util import now_utc

log = logging.getLogger("llm")

KERNEL_SLUG = os.environ.get("KAGGLE_KERNEL_SLUG", "signal-engine-scorer")
MODEL_7B = os.environ.get("KAGGLE_MODEL", "qwen-lm/qwen2.5/transformers/7b-instruct/1")
MODEL_3B = os.environ.get("KAGGLE_MODEL_SMALL", "qwen-lm/qwen2.5/transformers/3b-instruct/1")
SIZE_ORDER = ["1-10", "11-50", "51-200", "201-1000", "1000+"]

SYSTEM = (
    "You are a B2B sales-intelligence analyst. You read one public item (a post, comment, job ad, news story, "
    "filing, launch, review, tender or website change) and decide whether it shows that a specific company may "
    "buy the client's offer soon. Be strict: most items are NOT leads. People selling the same service, job "
    "seekers, students, consumers, giveaways and generic advice are not leads. Never invent facts; use only the "
    "item text. Reply with one JSON object and nothing else."
)

SCHEMA = """{
 "is_lead": true|false,
 "signal_ids": [numbers from the candidate list that the text really shows],
 "company_name": "name or null",
 "company_domain": "domain.com or null",
 "resolution_confidence": "high|medium|low|unresolved",
 "person_name": "name of the poster/decision maker if stated, else null",
 "is_b2b": true|false,
 "country": "ISO-2 country of the company (US, GB, IN...) from location, currency, city or domain clues; UNKNOWN if no clue",
 "employee_estimate": "1-10|11-50|51-200|201-1000|1000+|unknown",
 "intent_score": 1-10,
 "icp_fit_score": 1-10,
 "trigger": "explicit_intent|pain|churn|hiring|funding|launch|leadership|expansion|tech|website|procurement|community|new_company",
 "evidence_quote": "exact words from the text, max 25 words",
 "decision_maker_role": "who to contact",
 "recommended_angle": "LinkedIn connection note to the decision maker, max 280 characters, plain and specific to the signal, no pitch",
 "can_sell_offer": "yes|maybe|no: can this company realistically buy the OFFER now (needs more B2B pipeline, can afford a small agency, not a competitor)",
 "can_sell_reason": "one short sentence why",
 "reject_reason": "short reason if is_lead is false, else null"
}"""

GUIDE = (
    "Scoring guide. intent_score: 9-10 = actively asking to buy or hire exactly this kind of service now; "
    "7-8 = clear pain or trigger the offer solves, likely buying soon; 5-6 = relevant event but no stated need; "
    "1-4 = weak, off-topic or not a buyer. icp_fit_score: how well the company matches the ICP (size, B2B, "
    "industry, country); unknown size from a small founder-led business usually fits small-company ICPs. "
    "resolution_confidence: high = company name and domain stated; medium = company name stated or clearly "
    "inferable; low = only a vague description; unresolved = no company. employee_estimate: use clues such as "
    "funding stage (pre-seed/seed usually 1-10 or 11-50), 'solo founder', 'my small agency', number of open roles. "
    "Score intent 9 or 10 only when the text itself shows a buyer actively looking for or needing this kind of service now."
)


def _item_block(row: dict) -> str:
    meta = row.get("meta") or {}
    extras = []
    for k in ("subreddit", "story_title", "publisher", "amount", "industry", "year_inc", "revenue_range", "state",
              "location", "open_roles", "sales_roles", "sales_titles", "competitor", "rating", "flags", "tech"):
        v = meta.get(k)
        if v not in (None, "", [], {}):
            extras.append(f"{k}: {v}")
    if meta.get("persons"):
        extras.append("people: " + "; ".join(f"{p.get('name')} ({'/'.join(p.get('roles') or [])})" for p in meta["persons"][:5]))
    body = (row.get("body") or "")[:1800]
    return (
        f"source: {row['source']} ({row['kind']})\n"
        f"date: {row.get('published_at') or row.get('first_seen_at')}\n"
        f"url: {row.get('url')}\n"
        f"known company: {row.get('company_name') or 'unknown'} / {row.get('company_domain') or 'unknown'}\n"
        + ("\n".join(extras) + "\n" if extras else "")
        + f"title: {row.get('title') or ''}\ntext: {body}"
    )


def build_prompt(cfg: dict, row: dict, signal_ids: list[int]) -> str:
    cat = catalog()["signals"]
    icp = cfg.get("icp") or {}
    cands = "\n".join(f"{i}: {cat[i]['name']}" for i in signal_ids if i in cat)
    return (
        f"CLIENT OFFER: {cfg.get('offer')}\n"
        f"OFFER CATEGORY: {', '.join(cfg.get('offer_category') or [])}\n"
        f"ICP: {icp.get('description')} B2B only: {icp.get('b2b_only', True)}. Max employees: {icp.get('max_employees', 'any')}. "
        f"Countries: {', '.join(icp.get('countries') or []) or 'any'}. Exclude: {', '.join(icp.get('exclude') or [])}.\n"
        f"BUYER TITLES: {', '.join(cfg.get('buyer_titles') or [])}\n"
        f"OUTREACH CHANNEL: {cfg.get('outreach_channel', 'email')} (write recommended_angle for this channel)\n\n"
        f"CANDIDATE SIGNALS (from keyword rules):\n{cands}\n\n"
        f"ITEM:\n{_item_block(row)}\n\n{GUIDE}\n\nReturn JSON exactly in this shape:\n{SCHEMA}"
    )


def select_batch() -> list[dict]:
    out = []
    for c in clients():
        cap = int(c.cfg.get("llm_daily_cap", 500))
        rows = db.q("""
            select m.id as mid, m.signal_ids, r.* from client_matches m join raw_items r on r.id = m.item_id
            where m.client=%s and m.llm_scored_at is null and m.queue in ('new','lane_a')
              and m.created_at > now() - interval '5 days'
            order by case m.trigger when 'explicit_intent' then 0 when 'churn' then 1 when 'pain' then 2
                                    when 'hiring' then 3 when 'leadership' then 4 when 'funding' then 5 else 6 end,
                     m.rule_score desc, m.id desc limit %s""", (c.name, cap))
        for r in rows:
            out.append({"mid": r["mid"], "client": c.name, "prompt": build_prompt(c.cfg, r, r["signal_ids"])})
    return out


def kaggle_ready() -> bool:
    return bool(os.environ.get("KAGGLE_API_TOKEN") or (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")))


def _kaggle(*args: str, check=True) -> str:
    r = subprocess.run(["kaggle", *args], capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        raise RuntimeError(f"kaggle {' '.join(args)} failed: {out[-1500:]}")
    return out


def push() -> dict:
    batch = select_batch()
    if not batch:
        log.info("nothing to score")
        return {"pushed": 0}
    user = os.environ.get("KAGGLE_USERNAME") or "hamzamunyr"
    work = ROOT / "build" / "kernel"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    payload = base64.b64encode(gzip.compress(json.dumps({"system": SYSTEM, "items": batch}).encode())).decode()
    src = (ROOT / "engine" / "kaggle_kernel" / "score.py").read_text().replace("__PAYLOAD__", payload)
    (work / "score.py").write_text(src)
    meta = {
        "id": f"{user}/{KERNEL_SLUG}", "title": KERNEL_SLUG, "code_file": "score.py", "language": "python",
        "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_tpu": False,
        "enable_internet": False, "dataset_sources": [], "competition_sources": [], "kernel_sources": [],
        "model_sources": [MODEL_7B, MODEL_3B], "machine_shape": os.environ.get("KAGGLE_MACHINE", "NvidiaTeslaP100"),
    }
    (work / "kernel-metadata.json").write_text(json.dumps(meta, indent=1))
    out = _kaggle("kernels", "push", "-p", str(work))
    log.info("pushed %d items: %s", len(batch), out.strip()[-300:])
    db.kv_set("llm_last_push", {"at": now_utc().isoformat(), "n": len(batch), "mids": [b["mid"] for b in batch]})
    return {"pushed": len(batch)}


def wait_and_pull(max_wait_min: int = 200) -> dict:
    user = os.environ.get("KAGGLE_USERNAME") or "hamzamunyr"
    ref = f"{user}/{KERNEL_SLUG}"
    t0 = time.time()
    status = ""
    while time.time() - t0 < max_wait_min * 60:
        status = _kaggle("kernels", "status", ref, check=False).lower()
        if "complete" in status or "error" in status or "cancel" in status:
            break
        time.sleep(60)
    log.info("kernel status: %s", status.strip()[-200:])
    out_dir = ROOT / "build" / "kernel_out"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    _kaggle("kernels", "output", ref, "-p", str(out_dir), check=False)
    res = out_dir / "results.jsonl"
    if not res.exists():
        log.warning("no results.jsonl (status: %s); files: %s", status.strip(), [p.name for p in out_dir.iterdir()])
        return {"ingested": 0, "status": status.strip()}
    return ingest(res)


JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_output(text: str) -> dict | None:
    m = JSON_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0)
    for cand in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def _num(v, default=0.0) -> float:
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return default


def decide(cfg: dict, m: dict, d: dict | None) -> dict:
    """Turn the LLM JSON into priority + review queue."""
    groups = catalog()["groups"]
    threshold = float(cfg.get("score_threshold", 6.0))
    if not d:
        return {"queue": "rejected", "reject_reason": "llm output unparseable", "priority": 0}
    icp = cfg.get("icp") or {}
    intent, fit = _num(d.get("intent_score")), _num(d.get("icp_fit_score"))
    trig = d.get("trigger") if d.get("trigger") in groups else (m.get("trigger") or "pain")
    age = max(0.0, (now_utc() - (m.get("published_at") or m.get("first_seen_at") or now_utc())).total_seconds() / 86400)
    g = groups.get(trig, groups["pain"])
    decay = math.pow(2, -age / g["half_life"])
    priority = round((0.45 * intent + 0.35 * fit + 0.20 * min(10, g["weight"])) * decay, 2)
    size = d.get("employee_estimate") or "unknown"
    reason = None
    if not d.get("is_lead"):
        reason = d.get("reject_reason") or "not a lead"
    elif icp.get("b2b_only", True) and d.get("is_b2b") is False:
        reason = "not B2B"
    elif icp.get("max_employees") and size in SIZE_ORDER:
        lo = {"1-10": 1, "11-50": 11, "51-200": 51, "201-1000": 201, "1000+": 1001}[size]
        if lo > int(icp["max_employees"]):
            reason = f"too big ({size})"
    countries = [c.upper() for c in (icp.get("countries") or [])]
    country = str(d.get("country") or "UNKNOWN").upper()
    if not reason and countries and country not in countries:
        reason = f"outside target countries ({country})"
    min_intent = float(cfg.get("min_intent", 0) or 0)
    if not reason and intent < max(5, min_intent):
        reason = f"intent {intent:.0f} below {max(5, min_intent):g}"
    if not reason and str(d.get("can_sell_offer", "yes")).lower().startswith("no"):
        reason = "cannot sell offer: " + str(d.get("can_sell_reason") or "")[:120]
    if not reason and age > g["stop_after"]:
        reason = "stale"
    conf = d.get("resolution_confidence") or "unresolved"
    if reason:
        queue = "rejected"
    elif conf in ("high", "medium") and intent >= 7 and fit >= 7 and priority >= threshold:
        queue = "auto_approved"
    elif priority >= threshold - 1 or intent >= 8:
        queue = "human_review"
    else:
        queue, reason = "rejected", f"below threshold ({priority})"
    return {"queue": queue, "reject_reason": reason, "priority": priority}


def ingest(path: Path) -> dict:
    cfgs = {c.name: c.cfg for c in clients()}
    n = ok = 0
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += 1
        mrows = db.q("select m.*, r.published_at, r.first_seen_at from client_matches m join raw_items r on r.id=m.item_id "
                     "where m.id=%s", (rec["mid"],))
        if not mrows:
            continue
        m = mrows[0]
        d = parse_output(rec.get("output", ""))
        dec = decide(cfgs.get(m["client"], {}), m, d)
        d = d or {}
        sig = [int(s) for s in (d.get("signal_ids") or []) if str(s).isdigit()] or m["signal_ids"]
        db.x("""update client_matches set llm_scored_at=now(), intent_score=%s, icp_fit_score=%s, employee_estimate=%s,
                company_name=coalesce(nullif(%s,''), company_name), company_domain=coalesce(nullif(%s,''), company_domain),
                resolution_confidence=%s, evidence_quote=%s, decision_maker_role=%s, recommended_angle=%s,
                signal_ids=%s, trigger=coalesce(%s, trigger), priority=%s, reject_reason=%s,
                queue=case when queue='lane_a' and %s='rejected' then 'rejected'
                           when queue='lane_a' and exported_at is not null then queue else %s end
                where id=%s""",
             (_num(d.get("intent_score"), None), _num(d.get("icp_fit_score"), None), d.get("employee_estimate"),
              (d.get("company_name") or "")[:200] if d.get("company_name") not in (None, "null") else "",
              (d.get("company_domain") or "")[:200] if d.get("company_domain") not in (None, "null") else "",
              d.get("resolution_confidence"), (d.get("evidence_quote") or "")[:500], d.get("decision_maker_role"),
              (d.get("recommended_angle") or "")[:300], sig, d.get("trigger"), dec["priority"], dec["reject_reason"],
              dec["queue"], dec["queue"], m["id"]))
        ok += 1
    log.info("ingested %d/%d results", ok, n)
    return {"ingested": ok, "lines": n}
