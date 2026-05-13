#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DIR = Path('/home/jimwong/session-memory/logs/audits')
ALERT_DIR = Path('/home/jimwong/session-memory/logs/alerts')
ALERT_DIR.mkdir(parents=True, exist_ok=True)

alerts = json.loads((AUDIT_DIR / 'audit_alerts_latest.json').read_text(encoding='utf-8'))
summary = json.loads((AUDIT_DIR / 'audit_summary_latest.json').read_text(encoding='utf-8'))
runtime_alert_path = ALERT_DIR / 'opencode_runtime_alert_latest.json'
runtime_alert = json.loads(runtime_alert_path.read_text(encoding='utf-8')) if runtime_alert_path.exists() else None
if runtime_alert and runtime_alert.get('generated_at'):
    try:
        generated_at = datetime.fromisoformat(runtime_alert['generated_at'].replace('Z', '+00:00'))
        age_seconds = max(0, int((datetime.now(timezone.utc) - generated_at).total_seconds()))
        if age_seconds > 3600:
            runtime_alert = None
    except Exception:
        runtime_alert = None

payload = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'severity': alerts.get('severity', 'unknown'),
    'checks_evaluated': alerts.get('checks_evaluated', 0),
    'issues': alerts.get('issues', []),
    'continuation_recheck': summary.get('continuation_recheck'),
    'runtime_alert': runtime_alert,
    'repeat_offender_count': (runtime_alert or {}).get('repeat_offender_count', 0),
    'repeat_offender_sessions': (runtime_alert or {}).get('repeat_offender_sessions', []),
    'manual_investigate_count': (runtime_alert or {}).get('manual_investigate_count', 0),
    'manual_investigate_sessions': (runtime_alert or {}).get('manual_investigate_sessions', []),
    'headline': None,
}

severity = payload['severity']
if runtime_alert:
    runtime_severity = runtime_alert.get('severity', 'unknown')
    order = {'ok': 0, 'unknown': 0, 'warning': 1, 'critical': 2}
    if order.get(runtime_severity, 0) > order.get(severity, 0):
        severity = runtime_severity
    if runtime_alert.get('issues') and runtime_severity not in {'ok', 'idle'}:
        payload['issues'].append({
            'level': runtime_severity,
            'check': 'opencode_runtime',
            'issues': runtime_alert.get('issues', []),
        })

payload['severity'] = severity
if severity == 'critical':
    payload['headline'] = 'Session Memory CRITICAL alert'
elif severity == 'warning':
    payload['headline'] = 'Session Memory WARNING alert'
else:
    payload['headline'] = 'Session Memory OK status'

latest = ALERT_DIR / 'alert_payload_latest.json'
archived = ALERT_DIR / f"alert_payload_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
archived.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(payload, ensure_ascii=False, indent=2))
