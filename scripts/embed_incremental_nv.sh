#!/bin/bash
# 增量回填 embedding_nv（2048, NIM nemotron）—— messages + memory_atoms + memory_scenarios
# 由 cron 每 10 分钟调用。flock 防重入；worker 数保守避免挤压交互查询额度。
# 2026-09-23: 原版只回填 messages，导致 KG 持续新增的 atoms/scenarios 嵌入欠账
#   （atoms 覆盖 98.3% -> 68.5%），embed_watchdog.py 抓到后补齐。
LOCK=/tmp/embed_incr_nv.lock
exec 9>"$LOCK"
flock -n 9 || { echo "$(date "+%F %T") already running"; exit 0; }
cd /home/jimwong || exit 1
# 每分钟新增消息很少，扫一轮就够；用 2 worker 避免抢交互查询额度
timeout 240 python3 backfill_nim_embed_par.py 0 2 >> /home/jimwong/nim_embed_cron.log 2>&1
echo "$(date "+%F %T") msgs done: $(tail -1 /home/jimwong/nim_embed_par.log)" >> /home/jimwong/nim_embed_cron.log
# atoms/scenarios 增量（同一把锁内串行，幂等 WHERE embedding_nv IS NULL）
timeout 240 python3 backfill_nim_atoms.py >> /home/jimwong/nim_embed_cron.log 2>&1
echo "$(date "+%F %T") atoms/scenarios done" >> /home/jimwong/nim_embed_cron.log
