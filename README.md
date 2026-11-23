# signal-engine

Free, automatic buying-intent signal system for The Pixel Serve and its clients. It collects public
signals (Reddit, Hacker News, news, SEC Form D, launches, job boards, Trustpilot, websites), matches them to
each client's niche pack, scores them with an open LLM on a free Kaggle GPU, and sends the best leads to a
Google Sheet every morning.

Built from the spec "Intent Signal System: All 114 Signals, What We Can Get and How".

## How it runs

| Workflow | When (Pakistan time) | What it does |
|---|---|---|
| `frequent` | every 3 hours | Reddit, HN, news + funding, launches, SEC Form D, then match and send Lane A leads |
| `daily` | 02:10 | job boards, Trustpilot reviews, (8-K, tenders if a client uses them), LLM scoring on Kaggle, export, health |
| `weekly` | Sunday 00:30 | job-board discovery (self-hosted SearXNG), tech + website change snapshots, cleanup, health report commit |
| `manual` | on demand | any command, e.g. `collect reddit hn`, `match --rebuild`, `export --unscored` |

Costs nothing: GitHub Actions (public repo = unlimited minutes), Supabase free Postgres
(project `signal-engine`, schema `signals`), Kaggle free GPU, Google Sheets.

**Lane A** (same day): strong explicit intent or churn posted in the last 48 hours, or fresh funding. Goes to the
sheet on the next 3-hour run, before LLM scoring.
**Lane B** (daily): everything else matched is scored by Qwen 2.5 7B on Kaggle (2x T4), then routed to
`auto_approved`, `human_review` or `rejected` (with a reason).

Until Kaggle credentials are added, the engine still sends Lane A plus the strongest rule-matched items
(marked `unscored`) so the sheet is never empty.

## Signal coverage (The Pixel Serve)

114 signals in `signals.yaml`. 105 have a free collector; 9 are kept but not collected
(21, 22, 23, 28, 30 need LinkedIn; 85 ad libraries; 94 and 110 are demand proxies; 111 government contracts).
The Pixel Serve config switches off 13 that never fit a B2B company under 50 employees (public-company 8-K
events, government tenders, Series B+, M&A, layoffs), leaving **93 active signals**.

## Secrets and variables (repo Settings > Secrets and variables > Actions)

| Name | Type | Status |
|---|---|---|
| `SUPABASE_REF` | variable | set |
| `CONTACT_EMAIL` | variable | set (SEC User-Agent) |
| `DB_PASSWORD` | secret | set (role `signal_engine`, schema `signals` only) |
| `SHEET_TOKEN` | secret | set (must match the Apps Script property) |
| `SHEET_WEBHOOK` | secret | add after deploying the Apps Script |
| `KAGGLE_USERNAME`, `KAGGLE_KEY` | secrets | add from kaggle.com > Settings > API > Create New Token |

## Daily use

1. Open the lead sheet each morning. Rows are one per company, sorted by priority; Timeline shows every signal.
2. Set **Status** (approved, sent, replied, meeting, rejected) and add notes. The engine never overwrites those two columns.
3. Push approved rows into your email / decision-maker system.
4. Weekly: read `docs/health.md` (source health, funnel, matches per signal). GitHub emails you if a run fails.

## Change what it looks for

Edit `clients/pixelserve.yaml` (keywords, competitors, communities, negative keywords, weights, disabled signals),
commit, then run the `manual` workflow with `match --rebuild` to re-match recent items.

## Add a client

See `docs/onboarding.md`. Short version: copy `clients/_template.yaml` to `clients/<name>.yaml`, fill it from the
intake answers, make a sheet with `apps_script/Code.gs`, add `SHEET_WEBHOOK_<NAME>` as a secret. Data is
collected once and filtered per client, so new clients cost no extra requests.

## Layout

```
engine/collectors/   one file per source (reddit, hn, news, launches, edgar_formd, edgar_8k, jobs,
                     job_discovery, reviews, stackexchange, tenders, websites)
engine/config.py     loads signals.yaml + client packs, compiles matchers
engine/match.py      rule matching, decay, Lane A
engine/llm.py        Kaggle batch push / pull, JSON parsing, priority and review queue
engine/kaggle_kernel/score.py   the GPU script (batch is embedded; no secrets on Kaggle)
engine/export.py     one-row-per-company export to Google Sheets
apps_script/Code.gs  Sheet receiver (paste into the sheet's Apps Script)
signals.yaml         all 114 signals, groups, weights, half-lives
clients/             one niche pack per client
```
