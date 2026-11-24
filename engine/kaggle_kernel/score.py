# Runs on a free Kaggle GPU. The batch is embedded below by `python -m engine score-push`,
# so this script needs no internet, no database and no secrets.
# Output: /kaggle/working/results.jsonl  ({"mid": ..., "output": "<model JSON>"} per line)
import base64
import glob
import gzip
import json
import os
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

PAYLOAD = "__PAYLOAD__"
TIME_BUDGET = 150 * 60  # seconds; stop early so output is always saved
BATCH = 16
MAX_NEW = 260

t0 = time.time()
data = json.loads(gzip.decompress(base64.b64decode(PAYLOAD)))
items = data["items"]
system = data["system"]
print(f"{len(items)} items to score")


def find_models():
    dirs = sorted({os.path.dirname(p) for p in glob.glob("/kaggle/input/**/config.json", recursive=True)})
    big = [d for d in dirs if "7b" in d.lower()]
    small = [d for d in dirs if "3b" in d.lower()]
    return big, small


big, small = find_models()
gpus = torch.cuda.device_count()
mem = sum(torch.cuda.get_device_properties(i).total_memory for i in range(gpus)) / 1e9 if gpus else 0
print(f"gpus={gpus} total_mem={mem:.1f}GB models big={big} small={small}")
path = big[0] if big and mem >= 28 else (small[0] if small else (big[0] if big else None))
if path is None:
    raise SystemExit("no model attached")
print("using", path)

tok = AutoTokenizer.from_pretrained(path)
tok.padding_side = "left"
if tok.pad_token is None:
    tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    path, torch_dtype=torch.float16 if gpus else torch.float32, device_map="auto" if gpus else None)
model.eval()

prompts = []
for it in items:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": it["prompt"]}]
    prompts.append(tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True))

order = sorted(range(len(items)), key=lambda i: len(prompts[i]))
out_path = "/kaggle/working/results.jsonl"
done = 0
with open(out_path, "w") as f:
    for s in range(0, len(order), BATCH):
        if time.time() - t0 > TIME_BUDGET:
            print("time budget reached")
            break
        idx = order[s:s + BATCH]
        enc = tok([prompts[i] for i in idx], return_tensors="pt", padding=True, truncation=True, max_length=3072)
        enc = {k: v.to(model.device) for k, v in enc.items()}
        with torch.no_grad():
            gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=False, pad_token_id=tok.pad_token_id)
        texts = tok.batch_decode(gen[:, enc["input_ids"].shape[1]:], skip_special_tokens=True)
        for i, t in zip(idx, texts):
            f.write(json.dumps({"mid": items[i]["mid"], "client": items[i]["client"], "output": t}) + "\n")
        f.flush()
        done += len(idx)
        if done % 80 == 0:
            print(f"{done}/{len(items)} in {time.time() - t0:.0f}s")
print(f"done {done}/{len(items)} in {time.time() - t0:.0f}s")
