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
out, err, _ = run(
    f"docker exec {CONTAINER} python3 -c \""
    f"import sys; sys.path.insert(0,'/app');"
    f"from src.services.recall import nim_embed_query, _NIM_KEYFILE;"
    f"v = nim_embed_query('看门狗探活');"
    f"print('KEYFILE=' + _NIM_KEYFILE);"
    f"print('DIM=' + str(len(v) if v else 0))\" 2>&1 | tail -2")
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

if problems:
    ts = datetime.now().strftime("%F %T")
    print(f"[{ts}] ❌ Session Memory 嵌入健康检查异常（{len(problems)} 项）")
    for p in problems:
        print(f"  - {p}")
    print(f"  key数={nkeys} nim维度={dim} 填充率="
          f"{f'{pct:.1f}%' if pct is not None else '未知'} 待办={pending}")
    sys.exit(1)
else:
    if os.getenv("WD_VERBOSE"):
        print(f"OK keys={nkeys} dim={dim} fill={pct:.1f}% pending={pending}")
    sys.exit(0)
