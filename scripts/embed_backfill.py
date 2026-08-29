import os, time, json, traceback
from pathlib import Path
import psycopg2
import urllib.request

BATCH_SIZE = int(os.getenv('EMB_BATCH_SIZE', '64'))
PAGE_SIZE = int(os.getenv('EMB_PAGE_SIZE', '200'))
STATE_PATH = os.getenv('EMB_STATE_PATH', '/app/scripts/embed_backfill_state.json')
MODEL_PATH = os.getenv('EMB_MODEL_PATH', '/models/bge-base-zh-v1.5')
LOG = os.getenv('EMB_LOG', '/app/scripts/embed_backfill.log')
# GPU embedding service (preferred). Empty = use local sentence_transformers.
EMB_HTTP_URL = os.getenv('EMB_HTTP_URL', 'http://10.100.1.15:8002/v1/embeddings')
EMB_MODEL_NAME = os.getenv('EMB_MODEL_NAME', 'bge-base-zh-v1.5')
LOCAL_MODEL = None

Path(LOG).parent.mkdir(parents=True, exist_ok=True)
Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
            f.flush()
    except Exception:
        pass

def load_state():
    try:
        return json.loads(Path(STATE_PATH).read_text())
    except FileNotFoundError:
        return {}

def save_state(state):
    Path(STATE_PATH).write_text(json.dumps(state, ensure_ascii=False, indent=2))

def truncate(c):
    if len(c) > 4000:
        return c[:2000] + '\n...\n' + c[-2000:]
    return c

def embed_http(texts):
    """Call GPU embedding service. Returns list[list[float]] or raises."""
    payload = json.dumps({"input": texts, "model": EMB_MODEL_NAME}).encode()
    req = urllib.request.Request(EMB_HTTP_URL, data=payload,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return [d["embedding"] for d in resp["data"]]

def embed_local(texts):
    global LOCAL_MODEL
    if LOCAL_MODEL is None:
        from sentence_transformers import SentenceTransformer
        LOCAL_MODEL = SentenceTransformer(MODEL_PATH, device='cpu', trust_remote_code=True)
        LOCAL_MODEL.max_seq_length = 512
    return LOCAL_MODEL.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=False,
                              show_progress_bar=False, convert_to_numpy=True).tolist()

def embed(texts):
    if EMB_HTTP_URL:
        try:
            return embed_http(texts)
        except Exception as e:
            log(f"http_embed_fail err={str(e)[:120]} -> local fallback")
    return embed_local(texts)

def run():
    state = load_state()
    start_id = state.get('last_id')
    total_done = int(state.get('total_done', 0))
    total_seen = int(state.get('total_seen', 0))
    log(f"start last_id={start_id} seen={total_seen} done={total_done} mode={'http:'+EMB_HTTP_URL if EMB_HTTP_URL else 'local'}")
    conn = psycopg2.connect('postgresql://postgres:***@postgres:5432/session_memory')
    conn.autocommit = False
    cur = conn.cursor()
    loop_start = time.time()
    batch_start = time.time()
    while True:
        sql = """
            SELECT id::text, content FROM messages
            WHERE content IS NOT NULL AND length(content) > 20 AND embedding IS NULL
        """
        params = []
        if start_id:
            sql += " AND id::text > %s"
            params.append(start_id)
        sql += " ORDER BY id LIMIT %s"
        params.append(PAGE_SIZE)
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
        if not rows:
            break
        ids = [r[0] for r in rows]
        texts = [truncate(r[1]) for r in rows]
        try:
            embs = embed(texts)
        except Exception as e:
            traceback.print_exc()
            log(f"batch_fail first_id={ids[0]} err={str(e)[:200]} -> per-item")
            embs = []
            for msg_id, text in zip(ids, texts):
                try:
                    embs.append(embed([text])[0])
                except Exception as e3:
                    log(f"skip id={msg_id} err={str(e3)[:150]}")
                    embs.append(None)
        updated = 0
        for msg_id, emb in zip(ids, embs):
            if emb is None:
                continue
            vec = [float(x) for x in emb if x == x]
            if not vec:
                continue
            cur.execute(
                "UPDATE messages SET embedding=%s::vector WHERE id=%s AND embedding IS NULL",
                (json.dumps(vec, separators=(',', ':')), msg_id)
            )
            updated += cur.rowcount
        conn.commit()
        total_done += updated
        total_seen += len(rows)
        start_id = ids[-1]
        state.update({'last_id': start_id, 'total_seen': total_seen, 'total_done': total_done})
        save_state(state)
        elapsed = time.time() - loop_start
        tps = total_done / (elapsed + 1e-9)
        if total_seen <= 2000 or time.time() - batch_start > 60:
            log(f"progress seen={total_seen} done={total_done} last={start_id} wall_min={elapsed/60:.1f} avg_tps={tps:.2f}")
            batch_start = time.time()
        if total_seen % 10000 < PAGE_SIZE:
            conn.close()
            conn = psycopg2.connect('postgresql://postgres:***@postgres:5432/session_memory')
            conn.autocommit = False
            cur = conn.cursor()
    conn.close()
    log(f"done state={json.dumps(state)}")

if __name__ == '__main__':
    run()
