#!/bin/bash
# KG Worker 监控脚本
# 每5分钟检查一次 kg_worker 状态，如果失败则重启

LOG_FILE="/tmp/kg_worker_watchdog.log"
API_URL="http://localhost:8000"

echo "[$(date)] KG Worker Watchdog 启动" >> $LOG_FILE

while true; do
    # 检查 kg_worker 是否在运行
    if ! pgrep -f "kg_worker.py" > /dev/null; then
        echo "[$(date)] kg_worker 未运行，启动中..." >> $LOG_FILE
        docker exec session_memory_api python3 /app/scripts/kg_worker.py >> /tmp/kg_worker.log 2>&1 &
    fi
    
    # 检查 pending 任务数量
    PENDING=$(curl -s "$API_URL/api/v1/admin/dashboard" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('capacity_snapshot',{}).get('kg_jobs_pending',0))" 2>/dev/null)
    
    if [ "$PENDING" -gt 100 ]; then
        echo "[$(date)] pending 任务过多: $PENDING，启动额外 worker" >> $LOG_FILE
        docker exec session_memory_api python3 /app/scripts/kg_worker.py >> /tmp/kg_worker.log 2>&1 &
    fi
    
    sleep 300  # 5分钟检查一次
done
