#!/usr/bin/env python3
"""Session Memory 嵌入健康看门狗 —— 专防【静默降级】。

要防的三类静默故障（都真实发生过）：
  1. key 文件丢失/为空  -> nim_embed_query 静默返回 None，全部检索悄悄回落 768，无报错
  2. NIM 查询不可用      -> 同上，静默
  3. 填充率不涨          -> 增量 cron 挂了

⚠ 本脚本【跑在 13 号机上】，所有命令本地执行（不要 ssh 到自己 —— 没配回环密钥，
   那会让整个检查静默失效，第一版就踩了这个坑）。

cron 每 30 分钟跑一次；只在异常时输出，正常时完全安静。
退出码：0 = 正常，1 = 有异常。
"""
import os
import subprocess
import time
import sys
import urllib.request
from datetime import datetime

CONTAINER = "session_memory_api"
PG = "session_memory_postgres"
KEYFILE = "/home/jimwong/session-memory-backend/data/memory/.nim_keys"
MIN_KEYS = 20
MIN_FILL_PCT = 60.0


def run(cmd, timeout=90):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def psql(sql, timeout=90):
    with open("/tmp/wd.sql", "w") as f:
        f.write(sql + "\n")
    return run(f"docker cp /tmp/wd.sql {PG}:/tmp/wd.sql >/dev/null && "
               f"docker exec {PG} psql -U postgres -d session_memory -t -A -f /tmp/wd.sql",
               timeout)


problems = []

# --- 1) key 池 ---
out, _, _ = run(f"wc -l < {KEYFILE} 2>/dev/null || echo 0")
try:
    nkeys = int(out.strip() or 0)
except ValueError:
    nkeys = 0
if nkeys < MIN_KEYS:
    problems.append(f"key 池只有 {nkeys} 个（下限 {MIN_KEYS}）—— 文件可能丢失或未挂载")

# --- 2) NIM 查询是否真能用（最关键的静默故障） ---
_probe_cmd = (
    f"docker exec {CONTAINER} python3 -c \""
    f"import sys; sys.path.insert(0,'/app');"
    f"from src.services.recall import nim_embed_query, _NIM_KEYFILE;"
    f"v = nim_embed_query('看门狗探活');"
    f"print('KEYFILE=' + _NIM_KEYFILE);"
    f"print('DIM=' + str(len(v) if v else 0))\" 2>&1 | tail -2")
out, err, _ = run(_probe_cmd)
# 2026-09-29: 冷进程单发探针(冷 TLS + 空 LRU)可能 >1.2s 而误报 dim=0。
# 实测 09-23/24/26 三次报警的前后 30 分钟探针都正常 → 失败再试 2 次确认。
for _retry in range(2):
    if "DIM=2048" in out:
        break
    time.sleep(2)
    out, err, _ = run(_probe_cmd)
keyfile_line = next((l for l in out.split("\n") if l.startswith("KEYFILE=")), "KEYFILE=?")
dim_line = next((l for l in out.split("\n") if l.startswith("DIM=")), "DIM=0")
try:
    dim = int(dim_line.split("=")[1])
except (ValueError, IndexError):
    dim = 0
if dim != 2048:
    problems.append(
        f"nim_embed_query 返回维度 {dim}（应为 2048）—— 检索会静默回落 768。{keyfile_line}")

# --- 3) 填充率 ---
sql = ("SELECT count(*), count(embedding_nv), "
       "count(*) FILTER (WHERE embedding_nv IS NULL AND content IS NOT NULL "
       "AND length(content) > 20) FROM messages;")
out, err, _ = psql(sql)
pct = None
pending = None
try:
    tot, nv, pend = [x.strip() for x in out.split("|")]
    tot, nv, pend = int(tot), int(nv), int(pend)
    embeddable = tot - pend
    pct = 100.0 * nv / embeddable if embeddable else 100.0
    pending = pend
    if pct < MIN_FILL_PCT:
        problems.append(f"embedding_nv 填充率 {pct:.1f}%（下限 {MIN_FILL_PCT}%）"
                        f"，待办 {pend} 条 —— 增量 cron 可能挂了")
except (ValueError, IndexError):
    problems.append(f"填充率查询失败: {out[:100]} {err[:100]}")

# --- 4) 服务健康 ---
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=15) as r:
        if r.status != 200:
            problems.append(f"health 返回 {r.status}")
except Exception as e:
    problems.append(f"health 不可达: {type(e).__name__}")

# --- 5) atoms / scenarios 的 nv 覆盖 ---
out, _, _ = psql("SELECT (SELECT count(*) FROM memory_atoms), "
                 "(SELECT count(embedding_nv) FROM memory_atoms), "
                 "(SELECT count(*) FROM memory_scenarios), "
                 "(SELECT count(embedding_nv) FROM memory_scenarios);")
try:
    at, atnv, sc, scnv = [int(x.strip()) for x in out.split("|")]
    if at and atnv / at < 0.80:
        problems.append(f"memory_atoms nv 覆盖 {100.0*atnv/at:.1f}%（低于 80%）")
    if sc and scnv / sc < 0.80:
        problems.append(f"memory_scenarios nv 覆盖 {100.0*scnv/sc:.1f}%（低于 80%）")
except (ValueError, IndexError):
    pass

# --- 6) 磁盘水位（备份 + WAL + 索引都在同一文件系统上）---
try:
    st = os.statvfs("/")
    used_pct = 100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks
    if used_pct >= 85.0:
        problems.append(f"磁盘使用率 {used_pct:.1f}%（阈值 85%）—— 备份/WAL/索引会写满")
    if used_pct >= 80.0:
        problems.append(f"磁盘使用率 {used_pct:.1f}%（预警 80%）")
except Exception as e:
    problems.append(f"磁盘水位检查失败: {type(e).__name__}")

# --- 7) HNSW 索引有效性（无效索引 = 向量检索退化成全表扫描，37s 一次）---
out, _, _ = psql("""SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE c.relname LIKE '%hnsw%' AND NOT i.indisvalid;""")
try:
    bad = [x.strip() for x in out.splitlines() if x.strip()]
    if bad:
        problems.append(f"{len(bad)} 个 HNSW 索引无效（向量检索会退化成全表扫描）: {', '.join(bad[:3])}")
    out2, _, _ = psql("""SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                         WHERE c.relname LIKE '%hnsw%' AND i.indisvalid;""")
    n_ok = int(out2.strip() or 0)
    if n_ok < 6:
        problems.append(f"HNSW 索引只有 {n_ok} 个有效（预期 6 个）—— 某些表会退化成顺序扫描")
except (ValueError, IndexError):
    pass

# --- 8) 召回延迟（超时预算 3s，超了等于召回被砍成 0 命中）---
try:
    t0 = time.time()
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/recall/bridge", timeout=10) as r:
        r.read()
    dt = time.time() - t0
    if dt > 3.0:
        problems.append(f"recall 端点耗时 {dt:.2f}s（超过 3s 预算）")
except urllib.error.HTTPError:
    pass          # 4xx 说明参数不对，不代表慢
except Exception as e:
    problems.append(f"recall 端点不可达: {type(e).__name__}")

if problems:
    ts = datetime.now().strftime("%F %T")
    print(f"[{ts}] ❌ Session Memory 嵌入健康检查异常（{len(problems)} 项）")
    for p in problems:
        print(f"  - {p}")
    print(f"  key数={nkeys} nim维度={dim} 填充率="
          f"{f'{pct:.1f}%' if pct is not None else '未知'} 待办={pending}")
    # 外发 Telegram 告警 —— 只写日志的告警等于没有告警（没人会去看日志）
    try:
        _body = "\n".join(f"- {p}" for p in problems)
        _body += ("\n\nkey数=%s nim维度=%s 填充率=%s 待办=%s" % (
            nkeys, dim, ("%.1f%%" % pct) if pct is not None else "未知", pending))
        subprocess.run(["/home/jimwong/sm_notify.sh",
                        "❌ Session Memory 健康检查异常（%d 项）" % len(problems), _body],
                       timeout=40, check=False)
    except Exception as _e:
        print("  (告警发送失败: %s)" % type(_e).__name__)
    sys.exit(1)
else:
    if os.getenv("WD_VERBOSE"):
        print(f"OK keys={nkeys} dim={dim} fill={pct:.1f}% pending={pending}")
    sys.exit(0)
