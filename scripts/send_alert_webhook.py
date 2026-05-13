#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ALERT_DIR = Path('/home/jimwong/session-memory/logs/alerts')
ALERT_DIR.mkdir(parents=True, exist_ok=True)
PAYLOAD_PATH = ALERT_DIR / 'alert_payload_latest.json'
RESULT_PATH = ALERT_DIR / 'alert_delivery_latest.json'


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args()


def main():
    args = parse_args()
    payload = json.loads(PAYLOAD_PATH.read_text(encoding='utf-8'))
    severity = payload.get('severity', 'unknown')
    webhook = os.environ.get('ALERT_WEBHOOK_URL', '').strip()

    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'severity': severity,
        'sent': False,
        'dry_run': args.dry_run,
        'reason': None,
    }

    if severity == 'ok':
        result['reason'] = 'severity is ok, no delivery needed'
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        result['reason'] = 'dry run only'
        result['payload'] = payload
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not webhook:
        result['reason'] = 'ALERT_WEBHOOK_URL not configured'
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(webhook, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode('utf-8', errors='ignore')
        result['sent'] = True
        result['http_status'] = getattr(resp, 'status', None)
        result['response_preview'] = body[:500]

    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
