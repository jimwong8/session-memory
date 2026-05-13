# LEGACY FALLBACK: metadata-based KG backfill entrypoint, retained only for manual repair/rollback
#!/usr/bin/env bash
set -Eeuo pipefail
cd /home/jimwong/session-memory
MODE_ARGS="${*:-}"
/usr/bin/docker exec session_memory_api python /app/scripts/backfill_kg.py ${MODE_ARGS}
