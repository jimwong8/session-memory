#!/bin/bash
# Watcher: when embedding backfill done, restore vLLM on 10.100.1.15
# Runs on 10.100.1.13 (session-memory host). Uses SSH to 10.100.1.15.
set -e
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 root@10.100.1.15"
LOG=/home/jimwong/session-memory-backend/scripts/embed_watcher.log
EMBED_DONE_MARK=/home/jimwong/session-memory-backend/scripts/embed_done.flag

log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG"; }

# Wait until embedding backlog is near zero
while true; do
  REMAIN=$(docker exec session_memory_postgres psql -U postgres -d session_memory -Atc \
    "SELECT count(*) FROM messages WHERE embedding IS NULL AND content IS NOT NULL AND length(content) > 20;" 2>/dev/null | head -1)
  EMBEDDED=$(docker exec session_memory_postgres psql -U postgres -d session_memory -Atc \
    "SELECT count(*) FILTER(WHERE embedding IS NOT NULL) FROM messages;" 2>/dev/null | head -1)
  log "watcher: remaining=$REMAIN embedded=$EMBEDDED"
  if [ "${REMAIN:-1}" = "0" ]; then
    log "embedding backlog cleared. restoring vLLM on 10.100.1.15"
    break
  fi
  sleep 300
done

# Stop embed-gpu container on 15
$SSH "docker stop embed-gpu 2>/dev/null; docker rm embed-gpu 2>/dev/null; echo embed-gpu stopped" || true

# Re-enable + start vLLM
$SSH "systemctl enable vllm-gfx906.service; systemctl start vllm-gfx906.service; echo vllm enabled+started" || true

# Wait for vLLM to be ready
for i in $(seq 1 30); do
  if curl -sS --max-time 5 http://10.100.1.15:8000/v1/models >/dev/null 2>&1; then
    log "vLLM restored and serving on :8000"
    touch "$EMBED_DONE_MARK"
    exit 0
  fi
  sleep 10
done
log "WARNING: vLLM did not come up within 5min. Check 10.100.1.15."
exit 1
