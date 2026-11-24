"""Write personalized LinkedIn messages with a self-hosted open-source LLM (Qwen 7B, llama.cpp, CPU).

Reads signals.outreach_msgs rows without messages, writes msg1 + 3 follow-ups back.
  python -m engine msg-gen --shard 0 --shards 10
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from . import db
from .cpu_llm import _model_path

log = logging.getLogger("msg_gen")

SYSTEM = """You write LinkedIn direct messages for Hamza, a GTM engineer who runs The Pixel Serve.
Offer: a done-for-you outbound system for small US companies. Hamza builds the target list from buying
signals, writes the copy, runs LinkedIn and cold email, and books qualified sales meetings into the
client's calendar. It costs less than one SDR salary and starts producing pipeline in weeks, not months.

The person ALREADY accepted a connection request with no note. You write 4 messages:
- msg1: first message. Open with their specific signal (the job post or event in the data), in your own words.
  Name the real cost of that situation (e.g. a new SDR takes 3 to 6 months to ramp, the pipeline gap while
  hiring, first sales hire without a system behind them). Then one line on what Hamza does. End with a soft,
  easy question. 280 to 450 characters.
- fu1 (3 days later): add a new useful idea specific to their market (who they sell to, one targeting angle).
  No "just checking in". 200 to 380 characters.
- fu2 (4 days later): offer a small free thing: a short list of accounts in their market showing buying
  signals right now, so they can judge the targeting first. 180 to 330 characters.
- fu3 (5 days later): polite close. Gently note what waiting costs (pipeline that goes to whoever reaches
  those buyers first) without pressure, leave the door open, include cal.com/hamzamunyr/30min. 150 to 300 characters.

Rules: plain, warm, human English. Use their first name once in msg1 only. No em dashes or en dashes, use
commas or periods. No hype words (revolutionary, game-changer, synergy, leverage). No fake facts, numbers
about them, or claims of past results for them. Never say "I saw you opened" or "Re:". Do not call Hamza a
founder. No emojis. No subject lines. No placeholders in brackets.
Return ONLY JSON: {"msg1": "...", "fu1": "...", "fu2": "...", "fu3": "..."}"""

EXAMPLE_IN = """first_name: Dana
company: MedTrans Go (healthcare appointment logistics platform, Atlanta, 26 staff)
title: Co-Founder & CEO
signal: hiring
evidence: Hiring a remote Sales Development Representative to book meetings with clinic managers."""

EXAMPLE_OUT = json.dumps({
    "msg1": "Hi Dana, noticed MedTrans Go is hiring an SDR to book meetings with clinic managers. The hard part is usually the 3 to 6 months before a new rep has a real list, tested copy and a rhythm. I build that outbound system done-for-you, so meetings start while you hire. Is the SDR role meant to start pipeline from zero or add to something already working?",
    "fu1": "One thought on clinics: practice managers respond far better to messages tied to a recent change, like a new location or a new provider joining, than to generic logistics pitches. That is the kind of signal I build lists around so every message has a real reason to exist.",
    "fu2": "Happy to put together a short list of clinics in your area that show one of those signals right now, free. You can judge the targeting before deciding anything. Want me to send it over?",
    "fu3": "I will leave it here. The clinics changing things this quarter will book with whoever reaches them first, so if outbound moves up the list, grab a time here: cal.com/hamzamunyr/30min. Wishing you a strong close to the year.",
})


def _clean(t: str) -> str:
    t = (t or "").replace("—", ", ").replace("–", ", ").replace(" ,", ",")
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.replace(",,", ",")


def _first(name: str) -> str:
    n = re.sub(r"^(dr\.?|mr\.?|ms\.?|mrs\.?)\s+", "", (name or "").strip(), flags=re.I)
    return n.split()[0].rstrip(",.") if n else "there"


def run(shard: int = 0, shards: int = 1, redo: bool = False) -> dict:
    from llama_cpp import Llama

    rows = db.q("select * from signals.outreach_msgs where (%s or msg1 is null) and id %% %s = %s order by id",
                (redo, shards, shard))
    if not rows:
        return {"written": 0}
    llm = Llama(model_path=_model_path(), n_ctx=4096, n_threads=os.cpu_count() or 4, verbose=False)
    done = 0
    for r in rows:
        user = (f"first_name: {_first(r['dm_name'])}\ncompany: {r['company']} ({r['city'] or 'US'}, "
                f"{r['employees']} staff)\ntitle: {r['dm_title']}\nsignal: {r['signal']}\nevidence: {r['evidence']}")
        for attempt in range(2):
            try:
                out = llm.create_chat_completion(
                    messages=[{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": EXAMPLE_IN}, {"role": "assistant", "content": EXAMPLE_OUT},
                              {"role": "user", "content": user}],
                    temperature=0.5, max_tokens=700, response_format={"type": "json_object"})
                d = json.loads(out["choices"][0]["message"]["content"])
                m = {k: _clean(d.get(k, "")) for k in ("msg1", "fu1", "fu2", "fu3")}
                if all(len(v) > 60 for v in m.values()):
                    if "cal.com/hamzamunyr/30min" not in m["fu3"]:
                        m["fu3"] += " If timing works: cal.com/hamzamunyr/30min"
                    db.x("update signals.outreach_msgs set msg1=%s, fu1=%s, fu2=%s, fu3=%s, gen_at=now() where id=%s",
                         (m["msg1"], m["fu1"], m["fu2"], m["fu3"], r["id"]))
                    done += 1
                    break
            except Exception as e:
                log.warning("row %s attempt %s failed: %s", r["id"], attempt, e)
    log.info("shard %s wrote %s/%s", shard, done, len(rows))
    return {"written": done, "rows": len(rows)}
