#!/usr/bin/env python3
"""Fast bge-m3 embedding backfill - 16-key parallel"""
import os, time, threading
from openai import OpenAI
import psycopg2
import redis
from psycopg2.extras import execute_values

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"
BASE_URL = "https://api.edgefn.net/v1"
MODEL = "BAAI/bge-m3"
BATCH = 20
MAX_TOTAL = 220000

KEYS = [
    "sk-syEFrinxzFq78jolC7Fa7eEa605b4dC8B9F4F717FdBa28Bf",
    "sk-PaPq01oEJPsfhxj58c44A8Aa237d488e99A141A4DcAcB1B0",
    "sk-9oep26R1GInewtcn449243Ba4dD14aB2B79f2032Ed3e1a93",
    "sk-8Cs2nQ9f2hCLHQoR2307Ef082a4944Ab8496B06bAaA38f4e",
    "sk-gRDyHSWBEtogghNRE13b2fD4D8F44f45A1599676EdF0Dd99",
    "sk-haUAIW9JfVl4XFbT76F4466717C44bCf9d484aF89034F08c",
    "sk-EbEqTPgflLKmstW27d24074e0076441490B0264d0b76DaF3",
    "sk-YJZDtvQv9P3E7F6bF97f5f88Eb3e45C68b8b1e6dE92dD708",
    "sk-OnaMuilL5Wl0Se25EbB05a277c18434fAd3b9394504a352d",
    "sk-a2jl3SrKbFLWZ9jm09F0E3Eb00E64550A33e80F083E31b60",
    "sk-HeE39d52E5VP7fM2A653CbA144B34a1bB288Ba825f72166d",
    "sk-qK8upXmgysTCPWS79c2fB8D76e0b4b0fBf57Cd30EfCc8b80",
    "sk-TUbnKYvUPgkK9Lzg066b47F747C54202A8360e1746831c89",
    "sk-75WuMqlMGRmqokN36a57F79c2c2d4d918fE00866A2190032",
    "sk-9oep26R1GInewtcn449243Ba4dD14aB2B79f2032Ed3e1a93",
    "sk-8Cs2nQ9f2hCLHQoR2307Ef082a4944Ab8496B06bAaA38f4e",
]

lock = threading.Lock()
R = redis.Redis(host="redis", port=6379, db=0, socket_timeout=3)
def _incr(metric, route, amount=1):
    try:
        key = f"llm:route:{metric}:embed:{route}"
        R.incrby(key, amount)
        R.expire(key, 86400)
    except Exception:
        pass
total_done = [0]
total_needed = [0]
start_time = time.time()
active_workers = set()

def worker(key_idx):
    key = KEYS[key_idx]
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=20)
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    while total_done[0] < MAX_TOTAL:
        cur.execute("""
            SELECT id, content FROM messages 
            WHERE embedding_m3 IS NULL AND role IN ('user','assistant')
            AND length(content) >= 10
            ORDER BY created_at DESC LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (BATCH,))
        rows = cur.fetchall()
        if not rows:
            break

        ids = [r[0] for r in rows]
        texts = [(r[1] or "")[:2000] for r in rows]

        embeddings = None
        for attempt in range(2):
            try:
                r = client.embeddings.create(model=MODEL, input=texts)
                embeddings = [d.embedding for d in r.data]
                break
            except Exception as e:
                err = str(e)
                if "429" in err:
                    time.sleep(2)
                    continue
                elif "403" in err:
                    print(f"  [W{key_idx}] 403, stopping", flush=True)
                    cur.close(); conn.close(); return
                else:
                    break
        
        if embeddings is None:
            conn.rollback()
            time.sleep(1)
            continue

        data = [(str(mid), emb) for mid, emb in zip(ids, embeddings)]
        execute_values(cur, """
            UPDATE messages AS m SET embedding_m3 = d.emb::vector
            FROM (VALUES %s) AS d(id, emb)
            WHERE m.id = d.id::uuid
        """, data, template="(%s::uuid, %s::vector)")
        conn.commit()
        _incr("hit", "BAAI/bge-m3", len(rows))

        with lock:
            total_done[0] += len(rows)

    cur.close()
    conn.close()

# Main
conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("SELECT count(*) FROM messages WHERE embedding_m3 IS NULL AND role IN ('user','assistant') AND length(content) >= 10")
total_needed[0] = cur.fetchone()[0]
cur.close()
conn.close()

print(f"Messages without bge-m3: {total_needed[0]}", flush=True)
print(f"Workers: {len(KEYS)} x batch={BATCH}", flush=True)

threads = []
for i in range(len(KEYS)):
    t = threading.Thread(target=worker, args=(i,))
    t.start()
    threads.append(t)

while any(t.is_alive() for t in threads):
    time.sleep(5)
    elapsed = time.time() - start_time
    rate = total_done[0] / elapsed if elapsed > 0 else 0
    pct = total_done[0] * 100.0 / total_needed[0] if total_needed[0] > 0 else 100
    eta = (total_needed[0] - total_done[0]) / rate if rate > 0 else 0
    print(f"  [{total_done[0]}/{total_needed[0]} {pct:.1f}%] rate={rate:.1f}/s eta={eta/60:.0f}min", flush=True)

for t in threads:
    t.join()

elapsed = time.time() - start_time
print(f"\nDone: {total_done[0]} in {elapsed/60:.1f}min ({total_done[0]/elapsed:.1f}/s)", flush=True)
