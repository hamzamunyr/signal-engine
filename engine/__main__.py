"""CLI: python -m engine <command>

  collect <group|collector...>   run collectors (groups: frequent, daily, weekly)
  match                          match new items to clients (rules + Lane A)
  export [--unscored]            send review-ready leads to Google Sheets
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
    elif cmd == "export":
        from .export import run
        res = run(include_unscored=True if "--unscored" in args else None)
    elif cmd == "score-push":
        from .llm import kaggle_ready, push
        res = push() if kaggle_ready() else {"skipped": "no Kaggle credentials"}
    elif cmd == "score-pull":
        from .llm import kaggle_ready, wait_and_pull
        res = wait_and_pull() if kaggle_ready() else {"skipped": "no Kaggle credentials"}
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
