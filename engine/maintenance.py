"""Housekeeping: health report (also keeps the public repo's schedules alive) and data retention."""
from __future__ import annotations

from . import db
from .config import ROOT
from .util import now_utc


def health_report() -> str:
    rows = db.q("""
        select source, count(*) as runs, sum(case when ok then 1 else 0 end) as ok_runs,
               sum(items_found) as found, sum(items_new) as new, max(run_at) as last_run,
               (array_agg(error order by run_at desc) filter (where error is not null))[1] as last_error
        from source_health where run_at > now() - interval '7 days' group by source order by source""")
    funnel = db.q("""
        select client, count(*) as matched,
               sum(case when lane='A' then 1 else 0 end) as lane_a,
               sum(case when llm_scored_at is not null then 1 else 0 end) as scored,
               sum(case when queue='auto_approved' then 1 else 0 end) as approved,
               sum(case when queue='human_review' then 1 else 0 end) as review,
               sum(case when queue='rejected' then 1 else 0 end) as rejected,
               sum(case when exported_at is not null then 1 else 0 end) as exported
        from client_matches where created_at > now() - interval '7 days' group by client""")
    by_signal = db.q("""
        select client, sid, count(*) as n,
               sum(case when queue in ('auto_approved','human_review','lane_a') then 1 else 0 end) as kept
        from client_matches, unnest(signal_ids) as sid
        where created_at > now() - interval '7 days' group by client, sid order by client, n desc""")
    raw = db.q("select count(*) as n from raw_items where first_seen_at > now() - interval '7 days'")[0]["n"]
    lines = [f"# Source health (last 7 days)", "", f"Updated {now_utc():%Y-%m-%d %H:%M} UTC. Raw items collected: {raw}.", "",
             "| Source | Runs | OK | Found | New | Last run | Last error |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        err = (r["last_error"] or "").replace("|", "/").replace("\n", " ")[:120]
        lines.append(f"| {r['source']} | {r['runs']} | {r['ok_runs']} | {r['found']} | {r['new']} | "
                     f"{r['last_run']:%m-%d %H:%M} | {err} |")
    lines += ["", "## Funnel per client", "", "| Client | Matched | Lane A | LLM scored | Approved | Review | Rejected | Sent to sheet |",
              "|---|---|---|---|---|---|---|---|"]
    for f in funnel:
        lines.append(f"| {f['client']} | {f['matched']} | {f['lane_a']} | {f['scored']} | {f['approved']} | {f['review']} | "
                     f"{f['rejected']} | {f['exported']} |")
    lines += ["", "## Matches by signal", "", "| Client | Signal | Matched | Kept |", "|---|---|---|---|"]
    for s in by_signal[:80]:
        lines.append(f"| {s['client']} | {s['sid']} | {s['n']} | {s['kept']} |")
    text = "\n".join(lines) + "\n"
    (ROOT / "docs" / "health.md").write_text(text)
    return text


def cleanup() -> dict:
    a = db.x("update raw_items set body = left(body, 300) where first_seen_at < now() - interval '90 days' "
             "and length(body) > 300 and id not in (select item_id from client_matches where queue in "
             "('auto_approved','human_review','lane_a'))")
    b = db.x("delete from raw_items where first_seen_at < now() - interval '45 days' and id not in "
             "(select item_id from client_matches)")
    c = db.x("delete from source_health where run_at < now() - interval '60 days'")
    return {"trimmed": a, "deleted_unmatched": b, "health_deleted": c}
