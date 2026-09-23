#!/bin/bash
# 增量回填 embedding_nv（2048, NIM nemotron）—— 替代已停用的 768 版本
# 由 cron 每 10 分钟调用。flock 防重入；4 worker 足够跟上增量（不挤压交互查询）。
LOCK=/tmp/embed_incr_nv.lock
exec 9>"$LOCK"
flock -n 9 || { echo "$(date "+%F %T") already running"; exit 0; }
cd /home/jimwong || exit 1
# 每分钟新增消息很少，扫一轮就够；用 2 worker 避免抢交互查询额度
timeout 540 python3 backfill_nim_embed_par.py 0 2 >> /home/jimwong/nim_embed_cron.log 2>&1
echo "$(date "+%F %T") done: $(tail -1 /home/jimwong/nim_embed_par.log)" >> /home/jimwong/nim_embed_cron.log
