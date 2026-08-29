import os, time, json, traceback
from pathlib import Path
import psycopg2
from sentence_transformers import SentenceTransformer

BATCH_SIZE = int(os.getenv('EMB_BATCH_SIZE', '128'))
PAGE_SIZE = int(os.getenv('EMB_PAGE_SIZE', '2000'))
STATE_PATH = os.getenv('EMB_STATE_PATH', '/home/jimwong/session-memory-backend/logs/embeddings/state.json')
DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
DB_PORT = os.getenv('DB_PORT', '5432')
DB = os.getenv('DB_NAME', 'session_memory')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASS = os.getenv('DB_PASS', 'postgres')
MODEL_PATH = os.getenv('EMB_MODEL_PATH', '/home/jimwong/session-memory-backend/data/models/bge-base-zh-v1.5')
LOG = os.getenv('EMB_LOG', '/home/jimwong/session-memory-backend/logs/embeddings/backfill.log')

Path(LOG).parent.mkdir(parents=True, exist_ok=True)
Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')

def load_state():
    try:
        return json.loads(Path(STATE_PATH).read_text())
    except FileNotFoundError:
        return {}

def save_state(state):
    Path(STATE_PATH).write_text(json.dumps(state, ensure_ascii=False, indent=2))

def truncate(c):
    return c[:4096] + '...' + c[-4096:] if len(c) > 8192 else c

def run():
    state = load_state()
    start_id = state.get('last_id')
    total_done = int(state.get('total_done', 0))
    total_seen = int(state.get('total_seen', 0))
    failed_ids = state.get('failed_ids', [])
    log(f"start last_id={start_id} seen={total_seen} done={total_done} log={LOG}")
    model = SentenceTransformer(MODEL_PATH, device='cpu', trust_remote_code=True)
    model.max_seq_length = 512
    log(f"model_loaded dim={model.get_embedding_dimension()}")
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    cur = conn.cursor()
    loop_start = time.time()
    batch_start = time.time()
    while True:
        sql = """
            SELECT id::text, content FROM messages
            WHERE content IS NOT NULL AND length(content) > 20
              AND (embedding IS NULL OR vector_dims(embedding) <> 768)
        """
        params = []
        if start_id:
            sql += " AND id::text > %s"
            params.append(start_id)
        sql += " ORDER BY id LIMIT %s"
        params.append(PAGE_SIZE)
        cur.execute(sql, params)
        rows = cur.fetchall()
        if not rows:
            break
        ids = [r[0] for r in rows]
        texts = [truncate(r[1]) for r in rows]
        try:
            embs = model.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=False, show_progress_bar=False, convert_to_numpy=True)
        except Exception as e:
            traceback.print_exc()
            failed_ids.append({'id': ids[0], 'error': str(e)[:200]})
            state.update({'last_id': ids[0], 'total_seen': total_seen + len(rows), 'total_done': total_done, 'failed_ids': failed_ids[-20:]})
            save_state(state)
            log(f"encode_fail first_id={ids[0]} err={str(e)[:200]}")
            raise
        updated = 0
        for msg_id, emb in zip(ids, embs):
            vec = [float(x) for x in emb.tolist() if x == x]
            cur.execute(
                "UPDATE messages SET embedding=%s::vector, updated_at=now() WHERE id=%s AND (embedding IS NULL OR vector_dims(embedding) <> 768)",
                (json.dumps(vec, separators=(',', ':')), msg_id)
            )
            updated += cur.rowcount
        conn.commit()
        total_done += updated
        total_seen += len(rows)
        start_id = ids[-1]
        state.update({'last_id': start_id, 'total_seen': total_seen, 'total_done': total_done, 'failed_ids': failed_ids[-20:]})
        save_state(state)
        elapsed = time.time() - loop_start
        tps = total_done / (elapsed + 1e-9)
        if total_seen <= 2000 or time.time() - batch_start > 60:
            log(f"progress seen={total_seen} done={total_done} last={start_id} wall_min={elapsed/60:.1f} avg_tps={tps:.2f}")
            batch_start = time.time()
        if total_seen % 10000 < PAGE_SIZE:
            conn.close()
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB, user=DB_USER, password=DB_PASS)
            conn.autocommit = False
            cur = conn.cursor()
    conn.close()
    log(f"done state={json.dumps(state)}")

if __name__ == '__main__':
    run()
