"""Collectors. Each module exposes `collect() -> (items, errors)` and a NAME.

Collectors never raise for a single failed query: they record the error and keep going, so one broken
source never stops a run. The runner logs every collector run to `source_health`.
"""
from __future__ import annotations

import importlib
import logging
import time

from .. import db

log = logging.getLogger("collect")

# group -> collector modules. Groups map to workflow schedules.
GROUPS = {
    "frequent": ["reddit", "hn", "news", "launches", "edgar_formd"],
    "daily": ["jobs", "reviews", "stackexchange", "edgar_8k", "tenders"],
    "weekly": ["job_discovery", "websites"],
}


def run(names: list[str]) -> dict:
    summary = {}
    for name in names:
        mod = importlib.import_module(f".{name}", __name__)
        t0 = time.time()
        try:
            items, errors = mod.collect()
        except Exception as e:  # collector-level crash
            log.exception("%s crashed", name)
            db.log_health(name, False, 0, 0, f"crash: {e}", time.time() - t0)
            summary[name] = {"ok": False, "error": str(e)}
            continue
        try:
            new_ids = db.insert_items(items)
        except Exception as e:
            log.exception("%s insert failed", name)
            db.log_health(name, False, len(items), 0, f"insert: {e}", time.time() - t0)
            summary[name] = {"ok": False, "error": str(e)}
            continue
        ok = bool(items) or not errors
        err = "; ".join(errors[:8]) if errors else None
        db.log_health(name, ok, len(items), len(new_ids), err, time.time() - t0)
        summary[name] = {"ok": ok, "found": len(items), "new": len(new_ids), "errors": len(errors)}
        log.info("%s: found=%d new=%d errors=%d (%.0fs)", name, len(items), len(new_ids), len(errors), time.time() - t0)
        if errors:
            log.info("%s first errors: %s", name, errors[:3])
    return summary
