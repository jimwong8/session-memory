#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/jimwong/session-memory
MODE="${1:-pending}"
SESSION_ID="${2:-}"

if [[ -n "$SESSION_ID" ]]; then
  curl -sS -X POST "http://127.0.0.1:8000/api/v1/sessions/${SESSION_ID}/summarize"
  exit 0
fi

if [[ "$MODE" == "pending" ]]; then
  python3 - <<'PY'
import requests
sessions = requests.get('http://127.0.0.1:8000/api/v1/sessions', params={'user_id':'jimwong','limit':100}).json()
for s in sessions:
    sid = s['id']
    status = requests.get(f'http://127.0.0.1:8000/api/v1/sessions/{sid}/summary-status').json()
    if not status['latest_summary_exists']:
        print(requests.post(f'http://127.0.0.1:8000/api/v1/sessions/{sid}/summarize').text)
PY
else
  echo "用法: run_summary_once.sh [pending|<session_id>]"
fi
