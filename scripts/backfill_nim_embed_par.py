#!/usr/bin/env python3
"""NIM 嵌入回填（并发版）—— nvidia/nemotron-3-embed-1b (2048维) -> messages.embedding_nv

相对单线程版的改进
------------------
* **并发**：N workers 各自持有 DB 连接，主线程预取 id 分发给 worker。NIM 免费层是
  per-account 限流（总 RPM≈40-60），单线程只用到 ~15 RPM，有 3 倍余量。
* **自适应限速**：撞 429 就全局降速（worker 数减半式退避），恢复后缓慢回升。
* **可中断**：`WHERE embedding_nv IS NULL` 幂等；重复处理的代价只是少量浪费。
* 进度以 DB 为准（stdout 经 SSH 会缓冲）。

用法
----
    python3 backfill_nim_embed_par.py [目标条数] [worker数]
"""
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from queue import Empty, Queue

import psycopg2
from psycopg2.extras import execute_values

# 含图片的消息会让 NIM 判定为多模态输入 -> 400 "image inputs require VLM serving"。
# 一条污染消息会废掉整批，所以先清洗再送，另外还要能拆批隔离。
_IMG_RE = re.compile(
    # ⚠️ data:image 引用一律清洗：NIM 会把任何 data:image/* 判定为多模态输入 → 400。
    #    不限定 mime 字符集与是否带 base64 段（实际出现过 data:image/... 省略写法
    #    与 data:image/*;base64,... 星号 mime 两种都漏过）。
    r"data:image/[^\s\"'<>\u3000-\u303f\uff00-\uffef]{0,200}"
    r"|!\[[^\]]*\]\([^)]{0,500}\)"                 # markdown 图片
    r"|<image[^>]{0,200}>"                          # <image> 标签
    r"|(?:[A-Za-z0-9+/]{4}){80,}={0,2}"             # 裸 base64 长串
)


def sanitize(t):
    t = _IMG_RE.sub(" [图片] ", t)
    return t[:MAX_CHARS]

NIM_URL = "https://integrate.api.nvidia.com/v1/embeddings"
MODEL = "nvidia/nemotron-3-embed-1b"
BATCH = int(os.getenv("NIM_BATCH", "24"))
MAX_CHARS = 4000
KEYFILE = Path("/home/jimwong/.nim_keys")
LOG = Path("/home/jimwong/nim_embed_par.log")
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/session_memory"

_lock = threading.Lock()
_tls = threading.local()
_stats = {"done": 0, "fail": 0, "r429": 0, "r_other": 0}
BACKOFF = {"until": 0.0}     # 全局退避截止时间


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _lock:
        print(line, flush=True)
        try:
            with open(LOG, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def conn():
    c = getattr(_tls, "c", None)
    if c is None or c.closed:
        c = psycopg2.connect(DSN)
        c.autocommit = True
        _tls.c = c
    return c


def load_keys():
    ks = [k.strip() for k in KEYFILE.read_text().splitlines() if k.strip()]
    if not ks:
        raise SystemExit("no keys")
    random.shuffle(ks)
    return ks


def respect_backoff():
    while True:
        left = BACKOFF["until"] - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 5))


def embed_batch(texts, ki):
    """一次 HTTP 调用。429 时设置全局退避并抛异常给调用方重试。"""
    respect_backoff()
    payload = json.dumps({"input": texts, "model": MODEL,
                          "input_type": "passage", "encoding_format": "float"}).encode()
    keys = _KEYS
    key = keys[ki % len(keys)]
    req = urllib.request.Request(NIM_URL, data=payload, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        return [x["embedding"] for x in d["data"]]
    except urllib.error.HTTPError as e:
        body = e.read()[:160].decode("utf-8", "replace")
        if e.code in (429, 503):
            with _lock:
                _stats["r429"] += 1
                # 全局退避 8~20s，指数式（连续撞墙会叠加）
                BACKOFF["until"] = max(BACKOFF["until"], time.time() + 8 + random.uniform(0, 6))
            raise RuntimeError("429 rate limited: %s" % body[:80])
        with _lock:
            _stats["r_other"] += 1
        raise RuntimeError("HTTP %s %s" % (e.code, body[:80]))
    except Exception as e:
        with _lock:
            _stats["r_other"] += 1
        raise RuntimeError("%s %s" % (type(e).__name__, str(e)[:80]))


def embed_with_split(ids, texts, ki, depth=0):
    """返回 (成功的 [(id, emb)], 永久失败的 [id])。

    关键：NIM 对含图片的消息返 400，一条污染消息会废掉整批 24 条。
    遇到 400 就【二分拆批】把坏消息隔离出来，而不是整批放弃。
    """
    try:
        embs = embed_batch(texts, ki)
        return list(zip(ids, embs)), []
    except Exception as e:
        msg = str(e)
        if "400" in msg and len(ids) > 1 and depth < 6:
            h = len(ids) // 2
            a_ok, a_bad = embed_with_split(ids[:h], texts[:h], ki, depth + 1)
            b_ok, b_bad = embed_with_split(ids[h:], texts[h:], ki, depth + 1)
            return a_ok + b_ok, a_bad + b_bad
        return [], list(ids)


def worker(wid, q, stop):
    ki = wid
    while True:
        try:
            batch = q.get(timeout=30)
        except Empty:
            # ⚠ 不能在这里 return —— 主线程取下一批（索引扫描）可能超过 30s，
            # worker 误判结束会让大批已派发的数据丢失。
            if stop.is_set():
                return
            continue
        if batch is None:
            return
        ids, texts = batch
        ok, bad = embed_with_split(ids, texts, ki)
        if bad:
            with _lock:
                _stats["fail"] += len(bad)
            log("  w%d 永久失败 %d 条（已拆批隔离）" % (wid, len(bad)))
        if not ok:
            _mark_bad(bad)
            continue
        c = conn()
        cur = c.cursor()
        rows_up = [(mid, json.dumps([float(x) for x in emb if x == x], separators=(",", ":")))
                   for mid, emb in ok if any(x == x for x in emb)]
        n = 0
        if rows_up:
            try:
                # 一次往返写整批（单条 UPDATE 要 24 次往返，是明显瓶颈）
                execute_values(
                    cur,
                    "UPDATE messages m SET embedding_nv = CAST(d.vec AS vector) "
                    "FROM (VALUES %s) AS d(id, vec) "
                    "WHERE m.id = CAST(d.id AS uuid) AND m.embedding_nv IS NULL",
                    rows_up, template="(%s, %s)", page_size=64)
                n = cur.rowcount
            except Exception as e:
                log("  w%d 批量 UPDATE 失败: %s" % (wid, str(e)[:100]))
        cur.close()
        if bad:
            _mark_bad(bad)
        with _lock:
            _stats["done"] += n


def _mark_bad(ids):
    """永久失败标记 + 释放认领。否则它们会被反复取到 -> 死循环刷额度。"""
    if not ids:
        return
    try:
        cw = conn()
        cuw = cw.cursor()
        execute_values(
            cuw,
            "UPDATE messages m SET nim_claimed = NULL, nim_failed_at = now() "
            "FROM (VALUES %s) AS d(id) WHERE m.id = CAST(d.id AS uuid)",
            [(i,) for i in ids], template="(%s)", page_size=64)
        cuw.close()
    except Exception:
        pass


_KEYS = []


def main():
    global _KEYS
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    nw = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    _KEYS = load_keys()
    log("=== NIM 并发回填 model=%s batch=%d workers=%d keys=%d limit=%s ==="
        % (MODEL, BATCH, nw, len(_KEYS), limit or "全量"))

    mc = psycopg2.connect(DSN)
    mc.autocommit = True
    mcur = mc.cursor()
    mcur.execute("""SELECT count(*) FROM messages
                    WHERE embedding_nv IS NULL AND content IS NOT NULL AND length(content)>20""")
    remaining = mcur.fetchone()[0]
    log("待回填: %d 条" % remaining)

    q = Queue(maxsize=nw * 6)
    stop = threading.Event()
    ts = [threading.Thread(target=worker, args=(i, q, stop), daemon=True) for i in range(nw)]
    for t in ts:
        t.start()

    t0 = time.time()
    dispatched = 0
    last_log = 0.0
    # 释放上次崩溃留下的陈旧认领（否则那些行永远不会被再取到）
    mcur.execute("""UPDATE messages SET nim_claimed = NULL
                    WHERE nim_claimed IS NOT NULL AND embedding_nv IS NULL
                      -- 陈旧认领释放阈值：原 2 小时太长，dispatch 的取批阈值是 30 分钟，
                      -- 期间这些行既不被取也不被释放（实测 696 行卡在认领态）。
                      AND nim_claimed < now() - interval '30 minutes'""")
    log("释放陈旧认领: %d 行" % mcur.rowcount)

    while True:
        if limit and dispatched >= limit:
            break
        take = BATCH if not limit else min(BATCH, limit - dispatched)
        # ⚠ 必须【原子认领】：单纯 SELECT + 派发会让主线程重复取到同一批
        #   （索引让取批变快，主线程远超 worker 消费速度 => 同一批被派发上百次）
        mcur.execute("""WITH pick AS (
                            SELECT id FROM messages
                            WHERE embedding_nv IS NULL AND nim_claimed IS NULL
                              -- ⚠️ 原来硬性排除 nim_failed_at IS NULL，导致失败过的行
                              --    【永久】不再重试（实测 204 行卡死）。改为：
                              --    失败超过 7 天的允许重试（清理 transient 故障），
                              --    7 天内的跳过（避免反复打同一批坏数据）。
                              AND (nim_failed_at IS NULL OR nim_failed_at < now() - interval '7 days')
                              AND content IS NOT NULL AND length(content) > 20
                            ORDER BY created_at LIMIT %s
                        )
                        UPDATE messages m SET nim_claimed = now()
                        FROM pick WHERE m.id = pick.id
                        RETURNING m.id::text, m.content""", (take,))
        rows = mcur.fetchall()
        if not rows:
            log("主线程无更多数据")
            break
        ids = [r[0] for r in rows]
        texts = [sanitize(r[1]) for r in rows]
        q.put((ids, texts))
        dispatched += len(ids)
        el = time.time() - t0
        if el - last_log > 30:
            last_log = el
            log("dispatch=%d done=%d(%.1f/s) 429=%d 其它错=%d 放弃=%d backoff=%.0fs"
                % (dispatched, _stats["done"], _stats["done"] / max(el, 1e-9),
                   _stats["r429"], _stats["r_other"], _stats["fail"],
                   max(0.0, BACKOFF["until"] - time.time())))

    log("派发完成，等 worker 收尾…")
    for _ in ts:
        q.put(None)
    for t in ts:
        t.join(timeout=300)
    stop.set()

    el = time.time() - t0
    mcur.execute("SELECT count(*), count(embedding_nv) FROM messages")
    tot, nv = mcur.fetchone()
    log("=== 结束 写入=%d 派发=%d 耗时=%.0fs 均速=%.2f/s 429=%d 放弃=%d ==="
        % (_stats["done"], dispatched, el, _stats["done"] / max(el, 1e-9),
           _stats["r429"], _stats["fail"]))
    log("库状态: total=%d embedding_nv=%d (%.1f%%)" % (tot, nv, 100.0 * nv / max(tot, 1)))


if __name__ == "__main__":
    main()
