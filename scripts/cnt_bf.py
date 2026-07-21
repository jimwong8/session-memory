#!/usr/bin/env python3
import sys
sys.path.insert(0, "/app")
import psycopg2
from datetime import datetime, timezone

print(f"[{datetime.now(timezone.utc).isoformat()}] Backfill start", flush=True)

from src.embedding_service import _init_local, _encode
_init_local()
print("Model loaded", flush=True)

conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="session_memory", user="postgres", password="postgres")
cur = conn.cursor()
total = 0

while True:
    cur.execute("SELECT id, content FROM messages WHERE embedding IS NULL AND role IN ('user','assistant') LIMIT 100")
    rows = cur.fetchall()
    if not rows:
        break
    ids = [r[0] for r in rows]
    texts = [(r[1] or "")[:512] for r in rows]
    embs = _encode(texts)
    for mid, emb in zip(ids, embs):
        cur.execute("UPDATE messages SET embedding=%s::vector WHERE id=%s", (str(emb), mid))
    conn.commit()
    total += len(rows)
    if total % 500 == 0:
        print(f"  {total} done", flush=True)

conn.close()
print(f"DONE: {total} embeddings")
