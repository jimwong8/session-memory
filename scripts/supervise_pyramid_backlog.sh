#!/usr/bin/env bash
set -Eeuo pipefail

# 金字塔 backlog 监控 - 类似于 supervise_kg_jobs_backlog.sh
# 自动在需要时多次运行 run_pyramid_once.sh

PROJECT_DIR="/home/jimwong/session-memory"
RUNNER="$PROJECT_DIR/scripts/run_pyramid_once.sh"
DB_CONTAINER="session_memory_postgres"
DB_NAME="session_memory"
DB_USER="postgres"
MODE="${1:-}"

get_pending() {
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "
    SELECT COUNT(*) FROM sessions s
    WHERE s.message_count >= 5
    AND (
      SELECT COUNT(*) FROM memory_atoms a WHERE a.session_id = s.id
    ) * 5 < s.message_count;
  " 2>/dev/null || echo "0"
}

PENDING="$(get_pending)"
EXTRA_RUNS=0
if [ "$PENDING" -gt 200 ]; then
  EXTRA_RUNS=3
elif [ "$PENDING" -gt 50 ]; then
  EXTRA_RUNS=1
fi

echo "pending_sessions=$PENDING extra_runs=$EXTRA_RUNS dry_run=${MODE:-false}"

if [ "$MODE" = "--dry-run" ]; then
  exit 0
fi

for i in $(seq 1 "$EXTRA_RUNS"); do
  echo "compensation_run=$i"
  /usr/bin/env bash "$RUNNER"
done
