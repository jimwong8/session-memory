#!/usr/bin/env python3
"""Sync embedding backfill - optimized with multi-threading"""
import os, sys, json, time
os.chdir("/app")

import torch
torch.set_num_threads(24)

from transformers import BertTokenizer, BertModel, BertConfig
import psycopg2
from psycopg2.extras import execute_values

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
MODEL_PATH = os.environ.get("EMBEDDING_MODEL", "/models/bge-base-zh-v1.5")
BATCH_SIZE = 50
MAX_TOTAL = int(os.environ.get("BACKFILL_MAX", "220000"))

print(f"Loading {MODEL_PATH} (threads={torch.get_num_threads()})...", flush=True)
tokenizer = BertTokenizer(vocab_file=os.path.join(MODEL_PATH, "vocab.txt"))
state = torch.load(os.path.join(MODEL_PATH, "pytorch_model.bin"), map_location='cpu')
with open(os.path.join(MODEL_PATH, "config.json")) as f:
    cfg = json.load(f)
config = BertConfig(**cfg)
model = BertModel(config)
model.load_state_dict(state, strict=False)
model.eval()
dim = config.hidden_size
print(f"Loaded: dim={dim}", flush=True)

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT count(*) FROM messages WHERE embedding IS NULL AND role IN ('user','assistant')")
total_needed = cur.fetchone()[0]
print(f"Messages without embedding: {total_needed}", flush=True)

total = 0
start = time.time()
last_print = start

while total < MAX_TOTAL:
    cur.execute("""
        SELECT id, content FROM messages 
        WHERE embedding IS NULL AND role IN ('user','assistant')
        ORDER BY created_at DESC LIMIT %s
    """, (BATCH_SIZE,))
    rows = cur.fetchall()
    if not rows:
        print("No more messages without embeddings. Done!", flush=True)
        break

    ids = [r[0] for r in rows]
    texts = [(r[1] or "")[:2000] for r in rows]

    t0 = time.time()
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=256)
    with torch.no_grad():
        outputs = model(**inputs)
    mask = inputs["attention_mask"].unsqueeze(-1).expand(outputs.last_hidden_state.size()).float()
    embs = torch.sum(outputs.last_hidden_state * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)
    embeddings = embs.numpy()
    encode_time = time.time() - t0

    data = [(str(mid), emb.tolist()) for mid, emb in zip(ids, embeddings)]
    execute_values(cur, f"""
        UPDATE messages AS m SET embedding = d.emb::vector
        FROM (VALUES %s) AS d(id, emb)
        WHERE m.id = d.id::uuid
    """, data, template="(%s::uuid, %s::vector)")

    total += len(rows)
    now = time.time()
    elapsed = now - start
    batch_time = now - last_print
    overall_rate = total / elapsed if elapsed > 0 else 0
    pct = (total) * 100.0 / total_needed if total_needed > 0 else 100
    eta = (total_needed - total) / overall_rate if overall_rate > 0 else 0
    
    print(f"[{total:6d}/{total_needed} {pct:.1f}%] batch={len(rows)} encode={encode_time:.1f}s rate={len(rows)/batch_time:.1f}/s eta={eta/3600:.1f}h", flush=True)
    last_print = now

cur.close()
conn.close()
elapsed = time.time() - start
print(f"\nDone: {total} in {elapsed/3600:.1f}h", flush=True)
