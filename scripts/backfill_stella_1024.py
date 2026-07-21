#!/usr/bin/env python3
import os, torch, psycopg2
from datetime import datetime, timezone
from transformers import AutoTokenizer, AutoModel

path = "/models/stella"
print(f"[{datetime.now(timezone.utc).isoformat()}] Backfill start")

tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
model = AutoModel.from_pretrained(path, local_files_only=True)
print(f"Loaded: dim={model.config.hidden_size}")

def encode(texts):
    inputs = tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    with torch.no_grad():
        out = model(**inputs)
    m = inputs["attention_mask"].unsqueeze(-1).expand(out.last_hidden_state.size()).float()
    return (torch.sum(out.last_hidden_state * m, 1) / torch.clamp(m.sum(1), min=1e-9)).tolist()

conn = psycopg2.connect(host="localhost", dbname="session_memory", user="postgres", password="postgres")
cur = conn.cursor()
total = 0

while True:
    cur.execute("SELECT id, content FROM messages WHERE embedding IS NULL AND content IS NOT NULL AND role IN ("user", "assistant") LIMIT 50")
    rows = cur.fetchall()
    if not rows:
        break
    ids = [r[0] for r in rows]
    texts = [(r[1] or "")[:512] for r in rows]
    embs = encode(texts)
    for mid, emb in zip(ids, embs):
        cur.execute("UPDATE messages SET embedding=%s::vector WHERE id=%s", (str(emb), mid))
    conn.commit()
    total += len(rows)
    print(f"  {total} done", flush=True)

cur.execute("SELECT count(*) FROM messages WHERE embedding IS NOT NULL")
final = cur.fetchone()[0]
conn.close()
print(f"DONE: {total} new embeddings, total: {final}")

