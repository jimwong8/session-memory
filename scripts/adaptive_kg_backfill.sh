#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/home/jimwong/session-memory"
RUNNER="$PROJECT_DIR/scripts/run_kg_once.sh"
DB_CONTAINER="session_memory_postgres"
DB_NAME="session_memory"
DB_USER="postgres"
DRY_RUN="${1:-}"

get_pending() {
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "select count(*) from kg_jobs where status = 'pending';"
}

PENDING="$(get_pending)"
BATCH=5
SLEEP=0.5

if [ "$PENDING" -gt 500 ]; then
  BATCH=50
  SLEEP=0.05
elif [ "$PENDING" -gt 200 ]; then
  BATCH=25
  SLEEP=0.1
elif [ "$PENDING" -gt 50 ]; then
  BATCH=10
  SLEEP=0.2
fi

echo "pending=$PENDING batch_size=$BATCH sleep_seconds=$SLEEP dry_run=${DRY_RUN:-false}"

if [ "$DRY_RUN" = "--dry-run" ]; then
  exit 0
fi

/usr/bin/env bash "$RUNNER" --apply --batch-size "$BATCH" --sleep-seconds "$SLEEP"
