import os, time, json, traceback
from pathlib import Path
import psycopg2
from sentence_transformers import SentenceTransformer

BATCH_SIZE = int(os.getenv('EMB_BATCH_SIZE', '16'))
PAGE_SIZE = int(os.getenv('EMB_PAGE_SIZE', '50'))
SHARD_ID = int(os.getenv('EMB_SHARD_ID', '0'))
TOTAL_SHARDS = int(os.getenv('EMB_TOTAL_SHARDS', '1'))
MODEL_PATH = os.getenv('EMB_MODEL_PATH', '/models/bge-base-zh-v1.5')
LOG_DIR = os.getenv('EMB_LOG_DIR', '/app/scripts')
STATE_PATH = os.getenv('EMB_STATE_PATH', f'/app/scripts/embed_backfill_shard_{SHARD_ID}.json')
LOG = f'{LOG_DIR}/embed_backfill_shard_{SHARD_ID}.log'

Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [shard {SHARD_ID}/{TOTAL_SHARDS}] {msg}"
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

def run():
    state = load_state()
    start_id = state.get('last_id')
    total_done = int(state.get('total_done', 0))
    total_seen = int(state.get('total_seen', 0))
    log(f"start last_id={start_id} seen={total_seen} done={total_done}")
    model = SentenceTransformer(MODEL_PATH, device='cpu', trust_remote_code=True)
    model.max_seq_length = 512
    log(f"model_loaded dim={model.get_sentence_embedding_dimension()}")
    conn = psycopg2.connect('postgresql://postgres:***@postgres:5432/session_memory')
    conn.autocommit = False
    cur = conn.cursor()
    loop_start = time.time()
    batch_start = time.time()
    # shard boundaries: hex prefix ranges
    # uuid v4: 8-4-4-4-12 hex. Use first 2 hex chars for 256 shards, but TOTAL_SHARDS may be small.
    # Simpler: use modulo on id hash is not ordered; instead split by first char of uuid.
    # We'll use a range on the string id: each shard covers [lo, hi) in lexical order.
    # Compute boundaries from SHARD_ID/TOTAL_SHARDS over the full uuid space.
    # uuid chars: 0-9a-f. Treat as base-16. For TOTAL_SHARDS shards, each gets 16/TOTAL_SHARDS hex prefixes.
    hex_chars = '0123456789abcdef'
    step = 16 // TOTAL_SHARDS
    lo_idx = SHARD_ID * step
    hi_idx = (SHARD_ID + 1) * step if SHARD_ID + 1 < TOTAL_SHARDS else 16
    lo_prefix = hex_chars[lo_idx] if lo_idx < 16 else ''
    hi_prefix = hex_chars[hi_idx] if hi_idx < 16 else 'g'  # 'g' > 'f' ensures last shard covers all
    log(f"shard range: id >= '{lo_prefix}*' AND id < '{hi_prefix}*'")
    while True:
        sql = """
            SELECT id::text, content FROM messages
            WHERE content IS NOT NULL AND length(content) > 20 AND embedding IS NULL
              AND id::text >= %s AND id::text < %s
        """
        params = [lo_prefix, hi_prefix]
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
            embs = model.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=False, show_progress_bar=False, convert_to_numpy=True)
        except Exception as e:
            traceback.print_exc()
            log(f"batch_encode_fail first_id={ids[0]} err={str(e)[:200]} -> fallback")
            embs = []
            for msg_id, text in zip(ids, texts):
                try:
                    e2 = model.encode([text], batch_size=1, normalize_embeddings=False, show_progress_bar=False, convert_to_numpy=True)
                    embs.append(e2[0])
                except Exception as e3:
                    log(f"skip_embed id={msg_id} err={str(e3)[:150]}")
                    embs.append(None)
        updated = 0
        for msg_id, emb in zip(ids, embs):
            if emb is None:
                continue
            vec = [float(x) for x in emb.tolist() if x == x]
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
    conn.close()
    log(f"done state={json.dumps(state)}")

if __name__ == '__main__':
    run()
