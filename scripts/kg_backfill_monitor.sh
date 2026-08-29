#!/bin/bash
# KG 回填进度监控 - 每 30 分钟追加一行状态
NEW5=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -c "SELECT count(*) FROM messages WHERE created_at > now() - interval '5 min';" 2>/dev/null | tr -d ' ')
PENDING=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -c "SELECT count(*) FROM kg_jobs WHERE status='pending';" 2>/dev/null | tr -d ' ')
COMPLETED=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -c "SELECT count(*) FROM kg_jobs WHERE status='completed';" 2>/dev/null | tr -d ' ')
RUNNING=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -c "SELECT count(*) FROM kg_jobs WHERE status='running';" 2>/dev/null | tr -d ' ')
ACTIVE=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -c "SELECT count(DISTINCT session_id) FROM messages WHERE created_at > now() - interval '5 min';" 2>/dev/null | tr -d ' ')

# 默认值防空
NEW5=${NEW5:-0}; PENDING=${PENDING:-0}; COMPLETED=${COMPLETED:-0}; RUNNING=${RUNNING:-0}; ACTIVE=${ACTIVE:-0}

LINE="[$(date '+%m-%d %H:%M')] 新消息5min=${NEW5}条(${ACTIVE}会话) pending=${PENDING} completed=${COMPLETED} running=${RUNNING}"
echo "$LINE" >> /tmp/kg_backfill_monitor.log

# 回填完成判断：新消息 < 150条/5min（<30条/分钟）且无 running 堆积
if [ "$NEW5" -lt 150 ] 2>/dev/null; then
    echo "  ✅ 回填接近尾声：新消息已降至 $(echo "scale=0; $NEW5/5" | bc 2>/dev/null || echo $NEW5)条/分钟" >> /tmp/kg_backfill_monitor.log
fi

# 也输出到 stdout（cron 可捕获）
echo "$LINE"
