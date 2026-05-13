#!/usr/bin/env python3
import json
import sys
import urllib.request
from datetime import datetime, timezone

API = 'http://127.0.0.1:8000/api/v1/plugin-state/continuation/latest'
NOISE_MARKERS = [
    'TODO CONTINUATION',
    'OH-MY-OPENCODE',
    '[SYSTEM DIRECTIVE',
]


def main() -> int:
    with urllib.request.urlopen(API, timeout=20) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    value = payload.get('value') if isinstance(payload, dict) else None
    issues: list[str] = []
    status = 'ok'
    if not value:
        status = 'idle'
    else:
        last_user = value.get('last_user_message') or ''
        if any(marker in last_user for marker in NOISE_MARKERS):
            issues.append('last_user_message contains directive noise')
        recent = value.get('recent_messages') or []
        noisy_recent = [m for m in recent if any(marker in (m.get('content') or '') for marker in NOISE_MARKERS)]
        if noisy_recent:
            issues.append(f'recent_messages contain {len(noisy_recent)} noisy directive entries')
        if issues:
            status = 'degraded'
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': status,
        'issues': issues,
        'latest': value,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if status in {'ok', 'idle'} else 1


if __name__ == '__main__':
    sys.exit(main())
