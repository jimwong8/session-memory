#!/usr/bin/env bash
set -Eeuo pipefail
cd /home/jimwong/session-memory
MODE_ARGS="${*:-}"
/usr/bin/docker exec session_memory_api python /app/scripts/backfill_embeddings.py ${MODE_ARGS}
