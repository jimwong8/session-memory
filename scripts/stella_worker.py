#!/usr/bin/env python3
"""Stella 1024 worker - one-time load, batch process"""
import os, time, torch, psycopg2, json
from datetime import datetime, timezone
from safetensors.torch import load_file
from transformers import BertTokenizer, BertModel, BertConfig

path = "/models/stella"

# === 1. Load model once ===
print(f"[{datetime.now(timezone.utc).isoformat()}] Loading stella...", flush=True)
tokenizer = BertTokenizer(vocab_file=f"{path}/vocab.txt")

with open(f"{path}/config.json") as f:
    cfg = json.load(f)
config = BertConfig(**cfg)

model = BertModel(config)
state = load_file(f"{path}/model.safetensors")
state.pop("embeddings.position_ids", None)
model.load_state_dict(state, strict=False)
model.eval()
print(f"Loaded: dim={config.hidden_size}", flush=True)

def encode_batch(texts):
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    with torch.no_grad():
        out = model(**inputs)
    m = inputs["attention_mask"].unsqueeze(-1).expand(out.last_hidden_state.size()).float()
    return (torch.sum(out.last_hidden_state * m, 1) / torch.clamp(m.sum(1), min=1e-9)).tolist()

# === 2. Batch process loop ===
conn = psycopg2.connect(host="localhost", dbname="session_memory", user="postgres", password="postgres")
cur = conn.cursor()

total = 0
t0 = time.time()

while True:
    cur.execute("SELECT id, content FROM messages WHERE embedding IS NULL AND content IS NOT NULL AND role IN ('user','assistant') ORDER BY created_at ASC LIMIT 100")
    rows = cur.fetchall()
    if not rows:
        break
    ids = [r[0] for r in rows]
    texts = [(r[1] or "")[:512] for r in rows]
    embs = encode_batch(texts)
    for mid, emb in zip(ids, embs):
        cur.execute("UPDATE messages SET embedding=%s::vector WHERE id=%s", (str(emb), mid))
    conn.commit()
    total += len(rows)
    if total % 500 == 0:
        elapsed = time.time() - t0
        rate = total / elapsed
        cur.execute("SELECT count(*) FROM messages WHERE embedding IS NULL AND role IN ('user','assistant')")
        pending = cur.fetchone()[0]
        eta = pending / rate / 60 if rate > 0 else 0
        print(f"  {total} done ({rate:.1f}/s, ETA {eta:.1f}min)", flush=True)

cur.execute("SELECT count(*) FROM messages WHERE embedding IS NOT NULL")
final = cur.fetchone()[0]
conn.close()
print(f"DONE: {total} new, total {final}, took {time.time()-t0:.0f}s")
