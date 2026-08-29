#!/bin/bash
set -u
trap "exit 0" TERM INT
# Cloud KG worker (glm-4.5-air). Measured ceiling: 5 concurrent = 100% success
# and 40-45 jobs/min with zero HTTP 429. At 6+ the API rate-limits and
# effective throughput collapses, so KG_WORKERS must stay at 5.
# Runs alongside the local CPU worker; both claim rows via FOR UPDATE SKIP LOCKED.
while true; do
  /usr/bin/docker exec -e KG_BATCH=40 -e KG_WORKERS=5 \
    session_memory_api /usr/local/bin/python3 /app/scripts/kg_worker_glm_air.py \
    >> /tmp/kg_worker_glm.log 2>&1 || true
  sleep 3
done
