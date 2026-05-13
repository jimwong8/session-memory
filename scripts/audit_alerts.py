#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/home/jimwong/session-memory/logs/audits')
summary_path = BASE / 'audit_summary_latest.json'
summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
checks = summary.get('generated_files', [])
recheck = summary.get('continuation_recheck') or {}
continuation_latest_path = BASE / 'audit_continuation_latest.json'
continuation_latest = {}
if continuation_latest_path.exists():
    try:
        continuation_latest = json.loads(continuation_latest_path.read_text(encoding='utf-8'))
    except Exception:
        continuation_latest = {}

severity = 'ok'
issues = []

for item in checks:
    status = item.get('status')
    name = item.get('name')
    check_issues = item.get('issues', [])
    if name == 'audit_health' and status != 'ok':
        severity = 'critical'
        issues.append({'level': 'critical', 'check': name, 'issues': check_issues or ['health check degraded']})
    elif name in {'audit_duplicates', 'audit_continuation', 'audit_kg'} and status not in {'ok', 'idle'}:
        if severity != 'critical':
            severity = 'warning'
        issues.append({'level': 'warning', 'check': name, 'issues': check_issues})

continuation_has_latest = bool((continuation_latest or {}).get('latest'))
if continuation_has_latest and recheck.get('exists') and recheck.get('detected_change') is False:
    if severity == 'ok':
        severity = 'warning'
    issues.append({'level': 'warning', 'check': 'continuation_recheck', 'issues': [recheck.get('message')]})

report = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'severity': severity,
    'checks_evaluated': len(checks),
    'issues': issues,
}
print(json.dumps(report, ensure_ascii=False, indent=2))
