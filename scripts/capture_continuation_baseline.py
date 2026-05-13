#!/usr/bin/env python3
import json
import hashlib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = 'http://127.0.0.1:8000/api/v1/plugin-state/continuation/latest'
OUT_DIR = Path('/home/jimwong/session-memory/logs/audits')
OUT_DIR.mkdir(parents=True, exist_ok=True)

with urllib.request.urlopen(API, timeout=20) as resp:
    payload = json.loads(resp.read().decode('utf-8'))
value = payload.get('value') if isinstance(payload, dict) else None
key_material = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
report = {
    'captured_at': datetime.now(timezone.utc).isoformat(),
    'latest_exists': value is not None,
    'source_session_id': value.get('source_session_id') if value else None,
    'failure_code': value.get('failure_code') if value else None,
    'created_at': value.get('created_at') if value else None,
    'consumed_at': value.get('consumed_at') if value else None,
    'content_hash': hashlib.sha256(key_material.encode('utf-8')).hexdigest(),
    'latest': value,
}
(OUT_DIR / 'continuation_baseline_latest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
