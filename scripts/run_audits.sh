#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/home/jimwong/session-memory"
SCRIPT_DIR="$BASE_DIR/scripts"
LOG_DIR="$BASE_DIR/logs/audits"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"

run_one() {
  local name="$1"
  shift || true
  local target="$SCRIPT_DIR/$name"
  local latest="$LOG_DIR/${name%.py}_latest.json"
  local archived="$LOG_DIR/${name%.py}_$TS.json"
  local status_file="$LOG_DIR/${name%.py}_$TS.status"
  local tmp_json="$archived.tmp"
  local tmp_err="$status_file.err"

  rm -f "$tmp_json"
  if python3 "$target" "$@" > "$tmp_json" 2>"$tmp_err"; then
    mv "$tmp_json" "$archived"
    cp "$archived" "$latest"
    printf 'ok' > "$status_file"
  else
    code=$?
    mv "$tmp_json" "$archived" 2>/dev/null || printf '{"status":"parse_error","issues":["script produced no JSON output"],"error":"empty output"}\n' > "$archived"
    cp "$archived" "$latest" 2>/dev/null || true
    printf 'exit_code=%s' "$code" > "$status_file"
  fi
}

run_one audit_bridge.py
run_one audit_raw_events.py
write_summary() {
python3 - <<'PY'
from pathlib import Path
import json
base = Path('/home/jimwong/session-memory/logs/audits')
summary = {'generated_files': [], 'continuation_recheck': None}
for name in ['audit_health', 'audit_duplicates', 'audit_continuation', 'audit_kg', 'audit_trends', 'audit_capacity', 'audit_shared_memory', 'audit_opencode_runtime', 'audit_alerts', 'audit_ai_ops']:
    latest = base / f'{name}_latest.json'
    status = sorted(base.glob(f'{name}_*.status'))
    item = {'name': name, 'latest_exists': latest.exists()}
    if latest.exists():
        try:
            data = json.loads(latest.read_text(encoding='utf-8', errors='ignore'))
            item['status'] = data.get('status', data.get('severity', 'unknown'))
            issues = data.get('issues', [])
            if not issues and item['status'] not in {'ok', 'idle', 'unknown'}:
                if data.get('error'):
                    issues = [data['error']]
                elif data.get('summary'):
                    issues = [data['summary']]
                elif data.get('recommended_actions'):
                    issues = [a.get('action') for a in data.get('recommended_actions', []) if a.get('action')][:3]
            item['issues'] = issues
        except Exception as exc:
            item['status'] = f'parse_error: {exc}'
            item['issues'] = [str(exc)]
    if status:
        item['last_status_file'] = status[-1].name
        item['runner_status'] = status[-1].read_text(encoding='utf-8', errors='ignore')
    summary['generated_files'].append(item)
recheck = base / 'continuation_recheck_latest.json'
continuation_latest = base / 'audit_continuation_latest.json'
latest_value = None
if continuation_latest.exists():
    try:
        latest_value = json.loads(continuation_latest.read_text(encoding='utf-8', errors='ignore')).get('latest')
    except Exception:
        latest_value = None
if recheck.exists() and latest_value is not None:
    try:
        data = json.loads(recheck.read_text(encoding='utf-8', errors='ignore'))
        summary['continuation_recheck'] = {
            'exists': True,
            'detected_change': data.get('detected_change'),
            'message': data.get('message'),
        }
    except Exception as exc:
        summary['continuation_recheck'] = {'exists': True, 'parse_error': str(exc)}
else:
    summary['continuation_recheck'] = {'exists': False if not recheck.exists() else True, 'skipped': latest_value is None}
summary_path = base / 'audit_summary_latest.json'
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
}

run_one audit_health.py
run_one audit_duplicates.py
run_one audit_continuation.py
run_one audit_kg.py
run_one audit_trends.py
run_one audit_capacity.py
run_one audit_shared_memory.py
run_one audit_opencode_runtime.py
run_one audit_bridge.py
run_one audit_raw_events.py
write_summary >/dev/null
run_one audit_alerts.py
run_one audit_ai_ops.py
run_one audit_bridge.py
run_one audit_raw_events.py
write_summary
python3 "$SCRIPT_DIR/build_alert_payload.py" >/dev/null
python3 "$SCRIPT_DIR/send_alert_webhook.py" >/dev/null || true
