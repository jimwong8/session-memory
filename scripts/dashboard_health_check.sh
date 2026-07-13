#!/usr/bin/env bash
# Session Memory Dashboard 每日巡检
# 放在10.100.1.13的crontab中: 0 9 * * * /home/jimwong/session-memory/scripts/dashboard_health_check.sh

set -euo pipefail

API="http://localhost:8000/api/v1/admin/dashboard"
WEBHOOK="${ALERT_WEBHOOK_URL:-}"

# Fetch dashboard
RESP=$(curl -s "$API" 2>/dev/null || echo '{"overall_status":"error"}')
STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('overall_status','error'))" 2>/dev/null)

if [ "$STATUS" = "critical" ] || [ "$STATUS" = "error" ]; then
    # Get AI Ops advice
    ADVICE=$(echo "$RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
advice=d.get('ai_ops_advice',{})
print(f"风险: {advice.get('risk_level','?')}")
for a in advice.get('recommended_actions',[]):
    print(f"[{a.get('priority','?')}] {a.get('area','?')}: {a.get('action','?')[:100]}")
" 2>/dev/null)
    
    echo "[$(date)] WARNING: Dashboard status = $STATUS"
    echo "$ADVICE"
    
    # Send to webhook if configured
    if [ -n "$WEBHOOK" ]; then
        curl -s -X POST "$WEBHOOK" -H "Content-Type: application/json"             -d "{"text":"🔴 Session Memory: $STATUS\n$ADVICE"}" 2>/dev/null || true
    fi
elif [ "$STATUS" = "warning" ]; then
    echo "[$(date)] WARNING: Dashboard status = $STATUS (non-critical)"
else
    echo "[$(date)] OK: Dashboard status = $STATUS"
fi
