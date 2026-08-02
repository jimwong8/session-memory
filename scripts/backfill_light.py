#!/usr/bin/env python3
"""Lightweight backfill - 5 msgs per batch, 2s sleep"""
import time, requests, os, psycopg2
from psycopg2.extras import RealDictCursor

DB = "host=postgres dbname=session_memory user=postgres password=postgres"
SF_URL = "https://api.siliconflow.cn/v1"
SF_KEY = os.environ.get("SF_KEY", "sk-jvddfulkyqfzfozykrkcdwzyirscwsocomcgjmvrdejdzqui")

def embed(texts):
    try:
        r = requests.post(f"{SF_URL}/embeddings", json={"model": "BAAI/bge-base-zh-v1.5", "input": texts},
                         headers={"Authorization": f"Bearer {SF_KEY}"}, timeout=30)
        if r.status_code == 200:
            return [d["embedding"] for d in r.json()["data"]]
    except:
        pass
    return None

conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
conn.autocommit = False
cur = conn.cursor()
processed = 0

while True:
    cur.execute("""SELECT id, content FROM messages 
        WHERE embedding IS NULL AND role IN ('user','assistant') 
        AND length(content) > 20
        LIMIT 5""")
    rows = cur.fetchall()
    if not rows:
        break
    
    texts = [r["content"][:1000] for r in rows]
    embeddings = embed(texts)
    
    if embeddings:
        for row, emb in zip(rows, embeddings):
            cur.execute("UPDATE messages SET embedding=%s WHERE id=%s", (emb, row["id"]))
        conn.commit()
        processed += len(rows)
        print(f"  {processed} embedded", flush=True)
    else:
        conn.rollback()
        time.sleep(5)
    
    time.sleep(2)

conn.close()
print(f"Backfill done: {processed}")
