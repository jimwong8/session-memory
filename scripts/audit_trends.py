#!/usr/bin/env python3
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path('/home/jimwong/session-memory/logs/audits')
MAX_FILES = 8
trend = defaultdict(list)
for name in ['audit_health', 'audit_duplicates', 'audit_continuation', 'audit_kg']:
    files = sorted(BASE.glob(f'{name}_*.json'))[-MAX_FILES:]
    for f in files:
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        trend[name].append({
            'file': f.name,
            'status': data.get('status', 'unknown'),
            'issues': data.get('issues', []),
        })
status = 'ok' if trend else 'unknown'
report = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'status': status,
    'window_size': MAX_FILES,
    'trends': trend,
    'issues': [],
}
print(json.dumps(report, ensure_ascii=False, indent=2))
