#!/bin/bash
# Kg Worker Watchdog - 每5分钟检查一次

# 检查容器是否运行
if ! docker ps | grep session_memory_api > /dev/null; then
    echo "[$(date)] 容器未运行，启动中..." >> /tmp/kg_watchdog.log
    cd /home/jimwong/session-memory-backend && docker compose up -d
    sleep 30
fi

# 检查 pending 任务
PENDING=$(curl -sf http://localhost:8000/api/v1/admin/dashboard 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('capacity_snapshot',{}).get('kg_jobs_pending',0))" 2>/dev/null || echo "0")

if [ "$PENDING" -gt 100 ]; then
    echo "[$(date)] pending 任务过多: $PENDING" >> /tmp/kg_watchdog.log
fi
