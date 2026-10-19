#!/bin/bash
# Simple KG watchdog — direct DB query, no SSH hang, no false positive
# Runs every 5 minutes via cron

LOG_FILE="/tmp/kg_watchdog.log"
ALERT_THRESHOLD_PENDING=500
STALE_MINUTES=10

echo "$(date '+%Y-%m-%d %H:%M:%S') === watchdog start ===" >> "$LOG_FILE"

# 1. Stale running jobs → requeue
stale=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "
SELECT count(*) FROM kg_jobs WHERE status='running' AND locked_at < NOW() - interval '${STALE_MINUTES} minutes';
" 2>/dev/null | head -1)

if [ -n "$stale" ] && [ "$stale" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠ stale running: $stale, requeueing..." >> "$LOG_FILE"
    docker exec session_memory_postgres psql -U postgres -d session_memory -c "
UPDATE kg_jobs SET status='pending', locked_by=NULL, locked_at=NULL 
WHERE status='running' AND locked_at < NOW() - interval '${STALE_MINUTES} minutes';
" >> "$LOG_FILE" 2>&1
fi

# 2. Pending count alert
pending=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "
SELECT count(*) FROM kg_jobs WHERE status='pending';
" 2>/dev/null | head -1)

if [ -n "$pending" ] && [ "$pending" -gt "$ALERT_THRESHOLD_PENDING" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠ pending: $pending" >> "$LOG_FILE"
    /home/jimwong/sm_notify.sh "⚠️ KG pending 积压" "pending=$pending（阈值 $ALERT_THRESHOLD_PENDING）于 $(date '+%F %T')" 2>/dev/null || true
fi

# 3. Quarantined count
quarantined=$(docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "
SELECT count(*) FROM kg_jobs WHERE status='quarantined';
" 2>/dev/null | head -1)

if [ -n "$quarantined" ] && [ "$quarantined" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠ quarantined: $quarantined" >> "$LOG_FILE"
    /home/jimwong/sm_notify.sh "⚠️ KG quarantined 非零" "quarantined=$quarantined 于 $(date '+%F %T')" 2>/dev/null || true
fi

# 4. Summary
echo "$(date '+%Y-%m-%d %H:%M:%S') pending=$pending stale_running=$stale quarantined=$quarantined" >> "$LOG_FILE"
