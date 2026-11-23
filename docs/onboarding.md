# Onboarding a new client (about 2 hours)

1. Send the intake questions below. 
2. Draft the niche pack: copy `clients/_template.yaml` to `clients/<client>.yaml`. Ask Claude to turn the
   answers into 50 to 100 intent and pain phrases, negative keywords, subreddits, job titles, competitors and
   their domains, tech to watch and tender keywords.
3. Cut and fix the pack yourself, then get the client to approve it.
4. Make a Google Sheet for the client, paste `apps_script/Code.gs` into Extensions > Apps Script, set the
   script property `SHEET_TOKEN` (same value as the repo secret), deploy as a web app, and add the URL as repo
   secret `SHEET_WEBHOOK_<CLIENT>` (client name upper-case, dashes as underscores).
5. Commit, then run the `manual` workflow with `match --rebuild` to backfill the last days.
6. After 2 to 4 weeks, read `docs/health.md` ("Matches by signal"): disable signals that only produce rejects
   and raise the weight of the ones that produce meetings.

Weaker niches: if buyers rarely post online, posts (signals 1 to 20) are thin; hiring, funding, launches,
leadership and website changes do most of the work. Set `target_domains` to the client's account list so the
weekly tech and website tracker watches them.

## Intake questions

Business and offer
1. What do you sell, in one sentence?
2. What words would a buyer use to search for it?
3. What problem does it solve, and how do buyers describe that problem in their own words?
4. Average deal size and sales cycle?

Ideal customer
5. Industries? 6. Countries? 7. Company size? 8. Which titles buy, which use it?
9. Your 5 best customers (websites) and why they bought. 10. Who is a bad fit?

Competitors and market
11. Top 3 to 10 competitors? 12. What do buyers use before switching to you?
13. Which communities, forums or subreddits do buyers use? 14. Events or associations?

Buying triggers
15. What happens at a company right before it buys (hiring, funding, new leader, new tool, contract ending)?
16. Which job openings show they need you? 17. Do you sell to government (tender keywords)? 18. Seasonal peaks?

Outreach rules
19. Target account list? 20. Customers, open deals and contacts to exclude? 21. Leads per day or week?
22. Sheet or CRM? 23. Who reviews leads? 24. Words, claims or regions to avoid? 25. What is a good lead?
