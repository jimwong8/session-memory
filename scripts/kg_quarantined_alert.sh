#!/bin/bash
# Quarantined job 告警脚本 — 定期检测 kg_jobs 表中 status='quarantined' 数量
# 每 30 分钟通过 cron 运行，发现 quarantined 时输出告警

ALERT_THRESHOLD=${1:-1}
LOG_FILE="/tmp/kg_quarantined_alert.log"

count=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "
SELECT count(*) FROM kg_jobs WHERE status='quarantined';
" 2>/dev/null | head -1)

if [ -z "$count" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: cannot query DB" >> "$LOG_FILE"
    exit 1
fi

if [ "$count" -ge "$ALERT_THRESHOLD" ]; then
    sample=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "
SELECT left(last_error, 100) FROM kg_jobs WHERE status='quarantined' ORDER BY updated_at DESC LIMIT 1;
" 2>/dev/null | head -1)
    msg="$(date '+%Y-%m-%d %H:%M:%S') ALERT: quarantined=$count | latest: $sample"
    echo "$msg" >> "$LOG_FILE"
    /home/jimwong/sm_notify.sh "⚠️ KG quarantined 告警" "$msg" 2>/dev/null || true
    # 输出供 cron deliver 捕获
    echo "$msg"
    exit 0
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') OK: quarantined=$count" >> "$LOG_FILE"
    exit 0
fi
