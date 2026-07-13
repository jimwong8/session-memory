#!/usr/bin/env bash
set -Eeuo pipefail

LOCK_FILE="${LOCK_FILE:-/tmp/session-memory-adaptive-kg.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "已有自适应 KG drain 实例在运行，跳过本轮"
  exit 0
fi



WORKER=(/opt/conda/bin/python /app/scripts/backfill_kg_jobs.py --apply)
BATCH_SIZE="${BATCH_SIZE:-40}"
SLEEP_SECONDS="${SLEEP_SECONDS:-1.0}"
MAX_ROUNDS="${MAX_ROUNDS:-12}"
MAX_LOCKS="${MAX_LOCKS:-40}"
MIN_BATCH=4
MAX_BATCH=40
MAX_SLEEP=3.0
RATE_LIMIT_COOLDOWN=130
CONSECUTIVE_RATE_LIMITS=0

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_sql() {
  docker exec session_memory_postgres psql -U postgres -d session_memory -t -A -F '|' -c "$1"
}

queue_snapshot() {
  run_sql "select status, coalesce(last_error,'<none>'), count(*) from kg_jobs group by status, coalesce(last_error,'<none>') order by status, count desc;"
}

count_running_locks() {
  run_sql "select count(*) from kg_jobs where status='running';" | tr -d '[:space:]'
}

count_ready_pending() {
  run_sql "select count(*) from kg_jobs where status='pending' and coalesce(last_error,'<none>')='<none>';" | tr -d '[:space:]'
}

cleanup_stale_workers() {
  local pids
  pids=$(docker exec session_memory_api bash -lc "ps -ef | grep backfill_kg_jobs.py | grep -v grep | awk '{print \$2}' | xargs" || true)
  if [[ -n "${pids// }" ]]; then
    log "发现遗留 worker 进程: $pids，执行清理"
    docker exec session_memory_api bash -lc "kill $pids || true"
    sleep 2
    pids=$(docker exec session_memory_api bash -lc "ps -ef | grep backfill_kg_jobs.py | grep -v grep | awk '{print \$2}' | xargs" || true)
    if [[ -n "${pids// }" ]]; then
      docker exec session_memory_api bash -lc "kill -9 $pids || true"
    fi
  fi
  local running
  running=$(count_running_locks)
  if [[ "${running:-0}" != "0" ]]; then
    log "释放 running 锁: $running"
    run_sql "update kg_jobs set status='pending', locked_at=null, locked_by=null, updated_at=now() where status='running';" >/dev/null
  fi
}

adjust_after_rate_limit() {
  CONSECUTIVE_RATE_LIMITS=$((CONSECUTIVE_RATE_LIMITS + 1))
  if (( BATCH_SIZE > MIN_BATCH )); then
    BATCH_SIZE=$(( BATCH_SIZE / 2 ))
    if (( BATCH_SIZE < MIN_BATCH )); then BATCH_SIZE=$MIN_BATCH; fi
  fi
  SLEEP_SECONDS=$(python3 - <<PY
value = min($MAX_SLEEP, float($SLEEP_SECONDS) + 0.7)
print(f"{value:.2f}")
PY
)
  log "检测到限流，降载为 batch=${BATCH_SIZE} sleep=${SLEEP_SECONDS}s，冷却 ${RATE_LIMIT_COOLDOWN}s"
  sleep "$RATE_LIMIT_COOLDOWN"
}

adjust_after_clean_round() {
  CONSECUTIVE_RATE_LIMITS=0
  if (( BATCH_SIZE < MAX_BATCH )); then
    BATCH_SIZE=$(( BATCH_SIZE + 2 ))
    if (( BATCH_SIZE > MAX_BATCH )); then BATCH_SIZE=$MAX_BATCH; fi
  fi
  SLEEP_SECONDS=$(python3 - <<PY
value = max(0.35, float($SLEEP_SECONDS) - 0.1)
print(f"{value:.2f}")
PY
)
  log "本轮无明显限流，提速为 batch=${BATCH_SIZE} sleep=${SLEEP_SECONDS}s"
}

main() {
  log "启动自适应 KG backlog drain: batch=${BATCH_SIZE}, sleep=${SLEEP_SECONDS}, max_rounds=${MAX_ROUNDS}"
  cleanup_stale_workers

  for round in $(seq 1 "$MAX_ROUNDS"); do
    local_ready=$(count_ready_pending)
    local_running=$(count_running_locks)
    log "Round ${round}: ready_pending=${local_ready} running=${local_running}"
    if [[ -z "$local_ready" || "$local_ready" == "0" ]]; then
      log "没有 ready pending 任务，结束"
      break
    fi
    if (( local_running > MAX_LOCKS )); then
      log "running 锁过多(${local_running})，先清理再继续"
      cleanup_stale_workers
    fi

    output=$(docker exec session_memory_api "${WORKER[@]}" --batch-size "$BATCH_SIZE" --sleep-seconds "$SLEEP_SECONDS" 2>&1 || true)
    printf '%s\n' "$output"

    after_running=$(count_running_locks)
    if (( after_running > MAX_LOCKS )); then
      log "本轮后 running 锁异常(${after_running})，执行清理"
      cleanup_stale_workers
    fi

    if grep -qE 'rate_limit_break|rate_limited_upstream|rate_limited_cooldown|触发 429' <<<"$output"; then
      adjust_after_rate_limit
    else
      adjust_after_clean_round
    fi

    if (( CONSECUTIVE_RATE_LIMITS >= 3 )); then
      log "连续 3 轮限流，停止自动推进，等待人工复核"
      break
    fi
  done

  log '最终队列快照:'
  queue_snapshot
}

main "$@"
