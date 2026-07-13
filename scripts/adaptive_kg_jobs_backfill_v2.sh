#!/usr/bin/env bash
set -Eeuo pipefail

LOCK_FILE="${LOCK_FILE:-/tmp/session-memory-adaptive-kg-v2.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "已有 v2 实例在运行，跳过"
  exit 0
fi

WORKER=(/opt/conda/bin/python /app/scripts/backfill_kg_jobs_optimized.py --apply --use-batch-llm)
BATCH_SIZE="${BATCH_SIZE:-5}"
SLEEP_SECONDS="${SLEEP_SECONDS:-0.5}"
MAX_ROUNDS="${MAX_ROUNDS:-12}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

count_pending() {
  docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -c "select count(*) from kg_jobs where status='pending';" 2>/dev/null | tr -d '[:space:]'
}

main() {
  log "启动批量 KG backfill v2: batch=${BATCH_SIZE}, sleep=${SLEEP_SECONDS}, max_rounds=${MAX_ROUNDS}"
  
  rate_limit_count=0
  for round in $(seq 1 "$MAX_ROUNDS"); do
    pending=$(count_pending)
    log "Round ${round}: pending=${pending}"
    
    if [[ -z "$pending" || "$pending" == "0" ]]; then
      log "没有待处理任务，结束"
      break
    fi

    output=$(docker exec session_memory_api "${WORKER[@]}" --batch-size "$BATCH_SIZE" --sleep-seconds "$SLEEP_SECONDS" 2>&1 || true)
    printf '%s\n' "$output"

    if grep -qE '429|RateLimitExceeded|rate_limit' <<<"$output"; then
      rate_limit_count=$((rate_limit_count + 1))
      log "检测到限流 (${rate_limit_count}/3)，等待 60 秒"
      sleep 60
      
      if [[ $rate_limit_count -ge 3 ]]; then
        log "连续 3 次限流，降低 batch-size 至 3"
        BATCH_SIZE=3
        rate_limit_count=0
      fi
    else
      rate_limit_count=0
      sleep "$SLEEP_SECONDS"
    fi
  done
  log '完成'
}

main "$@"
