"""CLI: python -m engine <command>

  collect <group|collector...>   run collectors (groups: frequent, daily, weekly)
  match                          match new items to clients (rules + Lane A)
  resolve                        find website domains for leads that only have a name
  export [--unscored] [--rebuild]            send review-ready leads to Google Sheets
  score-push                     push today's LLM batch to Kaggle
  score-pull                     wait for Kaggle, ingest scores
  health                         write docs/health.md
  cleanup                        data retention
"""
from __future__ import annotations

import json
import logging
import sys


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "collect":
        from .collectors import GROUPS, run
        names: list[str] = []
        for a in args:
            names += GROUPS.get(a, [a])
        res = run(names)
    elif cmd == "match":
        from .match import run
        res = run(rebuild="--rebuild" in args)
    elif cmd == "resolve":
        from .resolve import run
        res = run()
    elif cmd == "export":
        from .export import run
        res = run(include_unscored=True if "--unscored" in args else None, rebuild="--rebuild" in args)
    elif cmd == "score-push":
        from .llm import kaggle_ready, push
        res = push() if kaggle_ready() else {"skipped": "no Kaggle credentials"}
    elif cmd == "score-pull":
        from .llm import kaggle_ready, wait_and_pull
        res = wait_and_pull() if kaggle_ready() else {"skipped": "no Kaggle credentials"}
    elif cmd == "score-cpu":
        from .cpu_llm import run
        kv = dict(zip(args[::2], args[1::2]))
        res = run(shard=int(kv.get("--shard", 0)), shards=int(kv.get("--shards", 1)),
                  limit=int(kv.get("--limit", 120)), minutes=float(kv.get("--minutes", 300)))
    elif cmd == "sql-file":
        from . import db
        from pathlib import Path
        rows = db.q(Path(args[0]).read_text())
        res = {"rows": len(rows or [])}
    elif cmd == "bulk":
        from .bulk import run
        res = run(args or ["workable", "reddit", "hn"])
    elif cmd == "probe":
        import requests
        UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
        for u in ["https://clutch.co/agencies/lead-generation", "https://clutch.co/profile/belkins",
                  "https://autocomplete.clearbit.com/v1/companies/suggest?query=Ten%20Forward%20Consulting",
                  "https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=sales&query=agency&after=2026-09-01&limit=5",
                  "https://jobs.workable.com/api/v1/jobs?query=sales%20development&location=United%20States",
                  "https://www.goodfirms.co/lead-generation-services", "https://www.g2.com/products/belkins/reviews",
                  "https://www.upcity.com/profiles/belkins"]:
            try:
                r = requests.get(u, headers=UA, timeout=30)
                print(r.status_code, len(r.text), u, r.text[:160].replace("\n", " "))
            except Exception as e:
                print("ERR", u, e)
        res = {}
    elif cmd == "score":
        from .llm import push, wait_and_pull
        res = {"push": push(), "pull": wait_and_pull()}
    elif cmd == "kstatus":
        from .llm import _kaggle
        from . import db
        out = {k: _kaggle("kernels", "status", f"hamzamunyr/{k}", check=False)[-300:]
               for k in ["signal-engine-scorer-0", "signal-engine-scorer-1", "signal-engine-scorer"]}
        db.kv_set("kstatus", out)
        res = out
    elif cmd == "kaggle":
        from .llm import _kaggle
        print(_kaggle(*args, check=False))
        res = {}
    elif cmd == "kaggle-smoke":
        from .llm import smoke
        res = smoke(machine=(args[0] if args else ""))
    elif cmd == "health":
        from .maintenance import health_report
        print(health_report())
        return 0
    elif cmd == "cleanup":
        from .maintenance import cleanup
        res = cleanup()
    else:
        print(__doc__)
        return 1
    print(json.dumps(res, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
