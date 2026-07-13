#!/bin/bash
# KG 积压简易监控脚本（在 Prometheus 配置前使用）

THRESHOLD_WARNING=500
THRESHOLD_CRITICAL=2000
LOG_FILE="/home/jimwong/session-memory/logs/kg_backlog_alerts.log"

# 查询当前积压
PENDING=$(docker exec session_memory_postgres psql -U postgres -d session_memory -At -c "SELECT COUNT(*) FROM kg_jobs WHERE status='pending';" 2>/dev/null)

if [ -z "$PENDING" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: 无法查询数据库" >> "$LOG_FILE"
  exit 1
fi

# 检查阈值
if [ "$PENDING" -gt "$THRESHOLD_CRITICAL" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRITICAL: KG jobs 积压 $PENDING (> $THRESHOLD_CRITICAL)" >> "$LOG_FILE"
  # 可以在这里添加通知逻辑（邮件、webhook 等）
elif [ "$PENDING" -gt "$THRESHOLD_WARNING" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: KG jobs 积压 $PENDING (> $THRESHOLD_WARNING)" >> "$LOG_FILE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] OK: KG jobs 积压 $PENDING" >> "$LOG_FILE"
fi
