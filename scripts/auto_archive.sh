#!/bin/bash
# 自动归档 30 天未活动会话
# 每天由 cron 执行

LOG=/var/log/session_memory_archive.log
DB_CONTAINER=session_memory_postgres

ARCHIVED=$(docker exec $DB_CONTAINER psql -U postgres -d session_memory -t -c "
UPDATE sessions SET archived = true
WHERE (archived = false OR archived IS NULL)
  AND id IN (
    SELECT s.id
    FROM sessions s
    LEFT JOIN messages m ON m.session_id = s.id
    GROUP BY s.id
    HAVING MAX(m.created_at) < NOW() - INTERVAL '30 days'
       OR (MAX(m.created_at) IS NULL AND s.created_at < NOW() - INTERVAL '30 days')
  )
RETURNING id;
" | grep -c '[a-f0-9-]\{36\}' || echo 0)

echo "$(date '+%Y-%m-%d %H:%M:%S') 归档了 $ARCHIVED 个 30 天未活动会话" >> $LOG
