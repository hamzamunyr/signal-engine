from datetime import datetime, timezone, timedelta
from engine.config import clients, catalog
from engine.match import match_row
from engine.llm import parse_output, decide, build_prompt

C = {c.name: c for c in clients()}["pixelserve"]
now = datetime.now(timezone.utc)

def row(kind, title, body, source="reddit", sub="sales", flags=None, **kw):
    return {"id": 1, "source": source, "kind": kind, "title": title, "body": body,
            "meta": {"subreddit": sub, "flags": flags or [], **kw.pop("meta", {})},
            "published_at": now - timedelta(hours=3), "first_seen_at": now, **kw}

cases = [
 ("explicit", row("post", "Looking for a lead generation agency for our SaaS", "We're a 12 person B2B SaaS, any good outbound agency you'd recommend? Budget 3k/mo"), True),
 ("pain", row("post", "Cold email not working anymore", "Our reply rate dropped to 0.5%, emails going to spam. small agency here"), True),
 ("seller noise", row("post", "I will provide b2b leads", "I help companies with lead generation, dm me for pricing"), False),
 ("jobseeker", row("post", "SDR interview tips?", "Looking for a job as SDR, how to become an sdr"), False),
 ("offtopic", row("post", "Best pizza in NYC", "anyone know"), False),
 ("outside community weak", row("post", "low reply rate on my cold emails", "help", sub="random"), False),
 ("outside community strong", row("post", "Can anyone recommend an appointment setting agency?", "need appointment setters for my roofing biz", sub="roofing"), True),
 ("job sdr", row("job", "Sales Development Representative", "SDR at Acme", source="jobs_greenhouse", flags=["first_sales_hire"]), True),
 ("job engineer", row("job", "Senior Software Engineer", "build stuff", source="jobs_greenhouse"), False),
 ("news seed", row("news", "Acme raises $2M seed round to automate invoices", "", source="google_news"), True),
 ("formd", row("filing", "Form D: Acme Inc raising 1500000", "", source="edgar_formd", flags=["form_d","form_d_small"]), True),
 ("8k skipped", row("filing", "8-K", "Item 5.02", source="edgar_8k", flags=["8k_5_02"]), False),
 ("review", row("review", "[Belkins] no results", "Review of Belkins (1 stars): waste of money, zero meetings", source="trustpilot", flags=["low_rating"], meta={"competitor":"Belkins"}), True),
 ("website", row("website", "x", "added: HubSpot", source="websites", flags=["tech_added_crm"]), True),
 ("launch", row("launch", "Acme - AI invoices", "", source="producthunt", flags=["launch_listing"]), True),
]
bad = 0
for name, r, want in cases:
    m = match_row(C, r)
    got = m is not None
    print(f"{'OK ' if got==want else 'BAD'} {name:28} -> {m}")
    bad += got != want
print("rules:", len(C.rules), "of", len(catalog()["signals"]))
# LLM output parsing + decision
d = parse_output('Sure! ```json\n{"is_lead": true, "intent_score": 9, "icp_fit_score": 8, "resolution_confidence": "medium", "employee_estimate": "11-50", "is_b2b": true, "trigger": "explicit_intent",}\n```')
print("parsed:", d)
print("decide:", decide(C.cfg, {"trigger": "explicit_intent", "published_at": now}, d))
print("decide big:", decide(C.cfg, {"trigger": "explicit_intent", "published_at": now}, {**d, "employee_estimate": "201-1000"}))
p = build_prompt(C.cfg, {**cases[0][1], "url": "u"}, [1,2,3])
print(len(p), "chars prompt")
assert bad == 0
