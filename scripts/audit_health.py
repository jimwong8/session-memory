#!/usr/bin/env python3
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path('/home/jimwong/session-memory')
BACKUP_DIR = BASE_DIR / 'backups' / 'migration-20260426'
MERGE_DIR = BACKUP_DIR / 'merge'
API_BASE = 'http://127.0.0.1:8000'


def run_psql(sql: str) -> str:
    cmd = [
        'docker', 'exec', 'session_memory_postgres',
        'psql', '-U', 'postgres', '-d', 'session_memory', '-At', '-c', sql,
    ]
    return subprocess.check_output(cmd, text=True).strip()


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def parse_key_count_rows(raw: str):
    data = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        key, value = line.split('|', 1)
        data[key] = int(value)
    return data


def main():
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'service_health': None,
        'db_counts': {},
        'duplicate_opencode_session_groups': None,
        'continuation_latest': None,
        'artifacts': {},
        'status': 'ok',
        'issues': [],
    }

    try:
        report['service_health'] = http_json(f'{API_BASE}/health')
        if report['service_health'].get('status') != 'ok':
            report['status'] = 'degraded'
            report['issues'].append('health endpoint not ok')
    except Exception as exc:
        report['status'] = 'error'
        report['issues'].append(f'health check failed: {exc}')

    try:
        counts_sql = "select 'sessions', count(*) from sessions union all select 'active_sessions', count(*) from sessions where archived=false union all select 'archived_sessions', count(*) from sessions where archived=true union all select 'messages', count(*) from messages union all select 'summaries', count(*) from summaries union all select 'kg_entities', count(*) from kg_entities union all select 'kg_relations', count(*) from kg_relations order by 1;"
        counts_raw = run_psql(counts_sql)
        report['db_counts'] = parse_key_count_rows(counts_raw)
    except Exception as exc:
        report['status'] = 'error'
        report['issues'].append(f'db counts failed: {exc}')

    try:
        dupes_sql = "select count(*) from (select metadata_json->>'opencode_session_id' from sessions where metadata_json ? 'opencode_session_id' group by 1 having count(*) > 1) d;"
        dupes = run_psql(dupes_sql)
        report['duplicate_opencode_session_groups'] = int(dupes or '0')
        if report['duplicate_opencode_session_groups'] > 0:
            report['status'] = 'degraded'
            report['issues'].append('duplicate opencode_session_id groups remain')
    except Exception as exc:
        report['status'] = 'error'
        report['issues'].append(f'duplicate audit failed: {exc}')

    try:
        latest = http_json(f'{API_BASE}/api/v1/plugin-state/continuation/latest')
        report['continuation_latest'] = latest.get('value') if isinstance(latest, dict) else latest
    except Exception as exc:
        report['status'] = 'degraded' if report['status'] == 'ok' else report['status']
        report['issues'].append(f'continuation latest check failed: {exc}')

    artifact_paths = {
        'migration_report': BACKUP_DIR / 'MIGRATION-REPORT.md',
        'pre_merge_backup': MERGE_DIR / 'pre-session-merge.sql',
        'merge_sql': MERGE_DIR / 'session_merge.sql',
        'duplicate_by_opencode': BACKUP_DIR / 'audit' / 'duplicate_sessions_by_opencode_session_id.tsv',
        'duplicate_by_title': BACKUP_DIR / 'audit' / 'duplicate_sessions_by_title.tsv',
    }
    for key, path in artifact_paths.items():
        report['artifacts'][key] = {
            'exists': path.exists(),
            'path': str(path),
            'size_bytes': path.stat().st_size if path.exists() else None,
        }
        if not path.exists():
            report['status'] = 'degraded' if report['status'] == 'ok' else report['status']
            report['issues'].append(f'missing artifact: {key}')

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['status'] == 'error':
        return 2
    if report['status'] == 'degraded':
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
