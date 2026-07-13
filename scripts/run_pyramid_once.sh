#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/home/jimwong/session-memory"
WORKER="$PROJECT_DIR/scripts/run_pyramid_worker.py"
DB_CONTAINER="session_memory_postgres"
DB_NAME="session_memory"
DB_USER="postgres"
DRY_RUN=false
SESSION_ARG=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --session=*) SESSION_ARG="${arg#--session=}" ;;
  esac
done

if [ "$DRY_RUN" = true ]; then
  echo "[dry-run] Checking pending sessions..."
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "
    SELECT s.id, s.message_count, s.title
    FROM sessions s
    WHERE s.message_count >= 5
    ORDER BY s.updated_at ASC
    LIMIT 5;
  " 2>&1 | while read -r line; do
    [ -z "$line" ] && continue
    echo "  candidate: $line"
  done
  echo "[dry-run] done"
  exit 0
fi

if [ -n "$SESSION_ARG" ]; then
  /usr/bin/docker exec session_memory_api python3 /app/scripts/run_pyramid_worker.py "$SESSION_ARG"
else
  /usr/bin/docker exec session_memory_api python3 /app/scripts/run_pyramid_worker.py
fi
