"""Self-hosted open-source LLM on GitHub Actions CPUs (no GPU queue, no API keys).

Runs Qwen2.5-3B-Instruct (GGUF, Q4_K_M) with llama.cpp. Each Actions job takes one shard of the
unscored matches, scores it with the same prompt as the Kaggle scorer and writes results straight
to the database. Used when the free Kaggle GPU is queued or as a daily top-up.

  python -m engine score-cpu --shard 0 --shards 4 --limit 120 --minutes 300
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from . import db
from .llm import SYSTEM, ingest, select_batch

log = logging.getLogger("cpu_llm")

REPO = os.environ.get("CPU_MODEL_REPO", "bartowski/Qwen2.5-7B-Instruct-GGUF")
FILE = os.environ.get("CPU_MODEL_FILE", "Qwen2.5-7B-Instruct-Q4_K_M.gguf")


def _model_path() -> str:
    from huggingface_hub import hf_hub_download
    return hf_hub_download(REPO, FILE)


def run(shard: int = 0, shards: int = 1, limit: int = 120, minutes: float = 300) -> dict:
    from llama_cpp import Llama

    batch = [b for b in select_batch() if b["mid"] % shards == shard][:limit]
    if not batch:
        log.info("nothing to score")
        return {"scored": 0}
    t0 = time.time()
    llm = Llama(model_path=_model_path(), n_ctx=4096, n_threads=os.cpu_count() or 4, verbose=False)
    log.info("model loaded in %.0fs; %d items for shard %d/%d", time.time() - t0, len(batch), shard, shards)
    out = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    done = 0
    with out.open("w") as f:
        for b in batch:
            if time.time() - t0 > minutes * 60:
                log.info("time budget reached")
                break
            try:
                r = llm.create_chat_completion(
                    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": b["prompt"]}],
                    temperature=0.0, max_tokens=420, response_format={"type": "json_object"})
                text = r["choices"][0]["message"]["content"]
            except Exception as e:  # one bad item must not stop the shard
                log.warning("item %s failed: %s", b["mid"], e)
                continue
            f.write(json.dumps({"mid": b["mid"], "client": b["client"], "output": text}) + "\n")
            f.flush()
            done += 1
            if done % 10 == 0:
                log.info("%d/%d scored (%.0fs)", done, len(batch), time.time() - t0)
    res = ingest(out)
    db.log_health(f"llm_cpu_shard{shard}", True, done, res.get("ingested", 0), None, time.time() - t0)
    return {"scored": done, **res}
