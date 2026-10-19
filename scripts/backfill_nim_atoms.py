#!/usr/bin/env python3
"""给 memory_atoms / memory_scenarios 补 NIM nemotron 向量（embedding_nv, 2048）。

口径（必须与现有实现一致，否则新旧向量不可比）：
  * atoms      -> 嵌 `content`              （atom_builder.py:378 `texts=[d["content"]]`）
  * scenarios  -> 嵌 `narrative_md[:2000]`  （scenario_builder.py:183 `create_embedding(narrative[:2000])`）
  * input_type -> "passage"（入库用 passage；检索用 query。用错会显著降召回）

⚠ 本脚本**不动** atoms 的旧 `embedding` 列：那条列同时服务于
   atom_builder 的向量去重（阈值 0.92，按 768 的分布标定）。去重留给旧列，
   nemotron 只用于检索 —— 换模型重标阈值收益为零、风险实在。

可中断/可重跑：WHERE embedding_nv IS NULL 幂等。
"""
import re as _re
import json
import os
import random
import sys
import threading
import time
import queue
import urllib.request
import psycopg2
from psycopg2.extras import execute_values

DSN = os.getenv("PGDSN", "postgresql://postgres:postgres@127.0.0.1:5432/session_memory")
NIM_URL = "https://integrate.api.nvidia.com/v1/embeddings"
MODEL = "nvidia/nemotron-3-embed-1b"
KEYFILE = os.getenv("NIM_KEYFILE", "/home/jimwong/.nim_keys")
BATCH = 16
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 0   # 0 = 全部

TABLES = {
    "memory_atoms":     {"col": "content",      "limit": 4000},
    "memory_scenarios": {"col": "narrative_md", "limit": 2000, "slice": 2000},
}

_q: "queue.Queue" = queue.Queue()
_stop = threading.Event()
_lock = threading.Lock()
_stats = {"done": 0, "fail": 0, "batch_err": 0}


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


# ⚠️ 含【图片字面量】的文本会被 NIM 判定为多模态输入 -> 400
#    "image inputs require VLM serving"。一条污染项会废掉整批（本脚本 BATCH=16），
#    表现为「派发 32 / 写入 0 / 批次失败 2」的静默不写。
#    正则与 backfill_nim_embed_par.py 的 sanitize() 完全一致。
_IMG_RE = _re.compile(
    # ⚠️ data:image 引用一律清洗：NIM 会把任何 data:image/* 判定为多模态输入 → 400。
    #    不限定 mime 字符集与是否带 base64 段（实际出现过 data:image/... 省略写法
    #    与 data:image/*;base64,... 星号 mime 两种都漏过）。
    r"data:image/[^\s\"'<>\u3000-\u303f\uff00-\uffef]{0,200}"
    r"|!\[[^\]]*\]\([^)]{0,500}\)"                 # markdown 图片
    r"|<image[^>]{0,200}>"                          # <image> 标签
    r"|(?:[A-Za-z0-9+/]{4}){80,}={0,2}"             # 裸 base64 长串
)


def sanitize(t: str) -> str:
    return _IMG_RE.sub(" [图片] ", t)[:4000]


def keys():
    ks = [k.strip() for k in open(KEYFILE) if k.strip()]
    random.shuffle(ks)
    return ks


KEYS = keys()


def embed(texts, wid):
    payload = json.dumps({"input": texts, "model": MODEL,
                          "input_type": "passage", "encoding_format": "float"}).encode()
    last = "unknown"
    for a in range(4):
        k = KEYS[(wid + a) % len(KEYS)]
        req = urllib.request.Request(NIM_URL, data=payload, headers={
            "Authorization": "Bearer " + k, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            return [x["embedding"] for x in d["data"]]
        except Exception as e:
            # HTTPError 的 body 才是真正的错误原因（str(e) 只有 "HTTP Error 400"），
            # 丢掉它会让 400/401/422 的根因完全不可见（2026-10-19 踩过）。
            body = ""
            try:
                body = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                body = ""
            msg = (str(e)[:120] + (" | " + body if body else "")).replace(chr(10), " ")[:300]
            if "429" in msg:
                time.sleep(2 * (a + 1))
            else:
                time.sleep(0.5 * (a + 1))
            last = msg
    log(f"  w{wid} 批次失败: {last}")
    return None


def worker(wid):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    while not _stop.is_set():
        try:
            item = _q.get(timeout=30)
        except queue.Empty:
            if _stop.is_set():
                return
            continue
        if item is None:
            return
        table, base_col, slice_n, ids = item
        cur = conn.cursor()
        # 取文本
        if slice_n:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id::text, left({base_col}, {slice_n}) FROM {table} "
                        f"WHERE id = ANY(%s::uuid[]) ORDER BY id", (ids,))
        else:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id::text, {base_col} FROM {table} "
                        f"WHERE id = ANY(%s::uuid[]) ORDER BY id", (ids,))
        rows = cur.fetchall()
        pairs = [(i, sanitize((t or "").strip())) for i, t in rows]
        pairs = [(i, t) for i, t in pairs if len(t) > 10]
        if not pairs:
            cur.close()
            continue
        embs = embed([t[:4000] for _, t in pairs], wid)
        if not embs or len(embs) != len(pairs):
            # 批次整体失败 → 二分拆批，把污染项隔离出来（否则一条坏项废掉整批）。
            ok_pairs, bad_ids = [], []
            _emb_ok, _emb_bad = [], []
            def _split(pl, depth=0):
                """二分拆批：成功返回 (embs, bad_ids)。"""
                if not pl:
                    return [], []
                try:
                    e = embed([t[:4000] for _, t in pl], wid)
                    if e and len(e) == len(pl):
                        return list(zip(pl, e)), []
                except Exception:
                    pass
                if len(pl) == 1 or depth >= 6:
                    return [], [pl[0][0]]
                h = len(pl) // 2
                a_ok, a_bad = _split(pl[:h], depth + 1)
                b_ok, b_bad = _split(pl[h:], depth + 1)
                return a_ok + b_ok, a_bad + b_bad
            ok_pairs, bad_ids = _split(pairs)
            if ok_pairs:
                with _lock:
                    _stats["done"] += len(ok_pairs)
            if bad_ids:
                with _lock:
                    _stats["fail"] += len(bad_ids)
                # 释放坏项认领，让后续重试（清洗规则更新后可能就好了）
                try:
                    cur.execute(f"UPDATE {table} SET nim_claimed=NULL WHERE id = ANY(%s::uuid[])",
                                (bad_ids,))
                except Exception:
                    pass
            pairs = ok_pairs
            if not pairs:
                cur.close()
                continue
            embs = [e for _, e in pairs]
        if not embs or len(embs) != len(pairs):
            with _lock:
                _stats["batch_err"] += 1
            # 释放认领，让后来者重试
            try:
                cur.execute(f"UPDATE {table} SET nim_claimed=NULL WHERE id = ANY(%s::uuid[])",
                            ([i for i, _ in pairs],))
            except Exception:
                pass
            cur.close()
            continue
        # 维度/有限性净化
        clean = []
        for (mid, _), v in zip(pairs, embs):
            if not v:
                continue
            vec = [float(x) for x in v if isinstance(x, (int, float))]
            if len(vec) != len(v):
                continue
            clean.append((json.dumps(vec, separators=(",", ":")), mid))
        # 用 VALUES 批量更新
        if clean:
            try:
                sql = (f"UPDATE {table} AS t SET embedding_nv = CAST(d.v AS vector) "
                       f"FROM (VALUES %s) AS d(v, id) "
                       f"WHERE t.id = CAST(d.id AS uuid) AND t.embedding_nv IS NULL")
                from psycopg2.extras import execute_values as ev
                with conn.cursor() as c2:
                    ev(c2, sql, clean, template="(%s, %s)", page_size=64)
                    n = c2.rowcount
            except Exception as e:
                log(f"  w{wid} 批量 UPDATE 失败: {str(e)[:120]}")
                n = 0
            with _lock:
                _stats["done"] += max(n, 0)
        cur.close()


def dispatch():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    total = 0
    for table, cfg in TABLES.items():
        base_col = cfg["col"]
        slice_n = cfg.get("slice")
        while True:
            if _stop.is_set():
                break
            # 原子认领（embedding_nv IS NULL 且未被认领）
            cur.execute(f"""
                WITH pick AS (
                    SELECT id FROM {table}
                    WHERE embedding_nv IS NULL
                      AND (nim_claimed IS NULL OR nim_claimed < now() - interval '30 minutes')
                      AND {base_col} IS NOT NULL
                      AND length({base_col}) > 10
                    ORDER BY id
                    LIMIT 200
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE {table} t SET nim_claimed = now()
                FROM pick WHERE t.id = pick.id
                RETURNING t.id::text
            """)
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                break
            for i in range(0, len(ids), BATCH):
                _q.put((table, base_col, slice_n, ids[i:i + BATCH]))
            total += len(ids)
            if TARGET and total >= TARGET:
                break
            if total % 2000 < 200:
                log(f"  派发中 [{table}] 合计={total} done={_stats['done']}")
    log(f"派发完成 合计={total}，等 worker 收尾…")
    cur.close()
    conn.close()
    return total


def main():
    t0 = time.time()
    log(f"=== start workers={WORKERS} target={TARGET or 'ALL'} ===")
    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
    for t in ths:
        t.start()
    total = dispatch()
    while not _q.empty() and time.time() - t0 < 3600:
        time.sleep(2)
    _stop.set()
    for _t in ths:
        _q.put(None)
    for t in ths:
        t.join(timeout=30)
    dt = time.time() - t0
    log(f"=== 结束 写入={_stats['done']} 派发={total} 批次失败={_stats['batch_err']} "
        f"耗时={dt:.0f}s 均速={_stats['done']/max(dt,1):.2f}/s ===")
    # 收尾统计
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    for table in TABLES:
        cur.execute(f"SELECT count(*), count(embedding_nv) FROM {table}")
        tot, nv = cur.fetchone()
        log(f"  {table}: total={tot} nv={nv} ({100.0*nv/max(tot,1):.1f}%)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
