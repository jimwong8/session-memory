#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/home/jimwong/session-memory"
KG_RUNNER="$PROJECT_DIR/scripts/adaptive_kg_jobs_backfill.sh"
DB_CONTAINER="session_memory_postgres"
DB_NAME="session_memory"
DB_USER="postgres"
MODE="${1:-}"

get_pending() {
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "select count(*) from kg_jobs where status = 'pending';"
}

PENDING="$(get_pending)"
EXTRA_RUNS=0
if [ "$PENDING" -gt 400 ]; then
  EXTRA_RUNS=2
elif [ "$PENDING" -gt 80 ]; then
  EXTRA_RUNS=1
fi

echo "pending=$PENDING extra_runs=$EXTRA_RUNS dry_run=${MODE:-false}"

if [ "$MODE" = "--dry-run" ]; then
  exit 0
fi

for i in $(seq 1 "$EXTRA_RUNS"); do
  echo "compensation_run=$i"
  /usr/bin/env bash "$KG_RUNNER"
done
