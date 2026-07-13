#!/usr/bin/env bash
set -euo pipefail
# collector-maintenance.sh - 按 last_acked_seq 裁剪 spool backlog + 归档死信
# 设计：读 collector-health.json → 按 session 裁剪 pending/retry 中已确认的事件
# 建议通过 systemd timer 每天运行一次

ROOT="/home/jimwong/session-memory/data/collector-spool"
HEALTH_FILE="/home/jimwong/session-memory/logs/collector/collector-health.json"
ARCHIVE_BASE="/home/jimwong/session-memory/data/collector-spool-archive"
TS=$(date +%Y%m%d_%H%M%S)
ARCHIVE="${ARCHIVE_BASE}/maintenance_${TS}"
MOVED=0

if [ ! -f "$HEALTH_FILE" ]; then
  echo "health file not found: $HEALTH_FILE"
  exit 1
fi

# 解析 last_acked_seq per session
declare -A ACKED
while IFS='|' read -r sid seq; do
  [ -n "$sid" ] && ACKED["$sid"]=$seq
done < <(python3 -c "
import json,sys
h=json.load(open('$HEALTH_FILE'))
for sid,v in (h.get('sessions') or {}).items():
    try: print(f\"{sid}|{int(v.get('last_acked_seq',0))}\")
    except: pass
")

mkdir -p "$ARCHIVE"

for bucket in pending retry deadletter; do
  d="$ROOT/$bucket"
  [ ! -d "$d" ] && continue
  for f in "$d"/*.json; do
    [ ! -f "$f" ] && continue
    sid=$(python3 -c "
import json; d=json.load(open('$f')); ev=d.get('event',{}); print(ev.get('session_id','') or '')
" 2>/dev/null || true)
    seq=$(python3 -c "
import json; d=json.load(open('$f')); ev=d.get('event',{}); print(int(ev.get('event_seq',0)))
" 2>/dev/null || true)
    ack=${ACKED[$sid]:-0}
    if [ -n "$sid" ] && [ "$ack" -gt 0 ] && [ "$seq" -le "$ack" ] 2>/dev/null; then
      t="$ARCHIVE/$bucket"
      mkdir -p "$t"
      mv "$f" "$t/"
      MOVED=$((MOVED+1))
    fi
  done
done

echo "maintenance $TS moved=$MOVED archive=$ARCHIVE"
