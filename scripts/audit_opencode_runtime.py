#!/usr/bin/env python3
"""OpenCode runtime audit: monitor active/stuck/error sessions from sqlite, plugin state and logs.
Supports local mode and SSH pull mode for desktop-hosted OpenCode data.
"""
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_OPENCODE = Path.home() / '.local' / 'share' / 'opencode'
BASE_DESKTOP = Path.home() / '.local' / 'share' / 'ai.opencode.desktop'
BASE_PLUGIN = Path.home() / '.cache' / 'session-memory'

DB_PATH = BASE_OPENCODE / 'opencode.db'
DB_WAL = BASE_OPENCODE / 'opencode.db-wal'
PLUGIN_DEBUG = BASE_PLUGIN / 'plugin-auto-debug.log'
PLUGIN_SESSIONS = BASE_PLUGIN / 'plugin-auto-sessions.json'
PLUGIN_MESSAGES = BASE_PLUGIN / 'plugin-auto-message-state.json'
PLUGIN_CONT = BASE_PLUGIN / 'plugin-auto-continuation.json'
DESKTOP_LOGS = BASE_DESKTOP / 'logs'
CLI_LOG_DIR = BASE_OPENCODE / 'log'

MODE = os.environ.get('OPENCODE_MONITOR_MODE', 'local')
SSH_HOST = os.environ.get('OPENCODE_MONITOR_HOST', '10.100.1.18')
SSH_USER = os.environ.get('OPENCODE_MONITOR_USER', 'jimwong')
SSH_PASS = os.environ.get('OPENCODE_MONITOR_PASSWORD', '')

STALE_ASSISTANT_SECONDS = 180
STALE_PLUGIN_SECONDS = 120
STALE_CONTINUATION_SECONDS = 300
MAX_LOG_SCAN_BYTES = 512_000


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_remote_python(script: str) -> str:
    if MODE != 'ssh':
        raise RuntimeError('remote helper called in local mode')
    base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=8', f'{SSH_USER}@{SSH_HOST}', 'python3', '-c', script]
    if SSH_PASS:
        if shutil.which('sshpass') is None:
            raise RuntimeError('sshpass unavailable for password-based ssh mode')
        cmd = ['sshpass', '-p', SSH_PASS] + base
    else:
        cmd = base
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def age_seconds_from_mtime(path: Path) -> int | None:
    mtime = safe_mtime(path)
    if mtime is None:
        return None
    return max(0, int(datetime.now().timestamp() - mtime))


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return fallback


def latest_lines(path: Path, limit: int = 200) -> list[str]:
    try:
        data = path.read_bytes()
        if len(data) > MAX_LOG_SCAN_BYTES:
            data = data[-MAX_LOG_SCAN_BYTES:]
        text = data.decode('utf-8', errors='ignore')
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-limit:]
    except Exception:
        return []


def newest_log_lines(directory: Path, limit: int = 200) -> list[str]:
    if not directory.exists():
        return []
    files = sorted([p for p in directory.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []
    return latest_lines(files[0], limit=limit)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None


def audit_sqlite_local() -> dict[str, Any]:
    report: dict[str, Any] = {
        'db_exists': DB_PATH.exists(),
        'wal_exists': DB_WAL.exists(),
        'db_mtime_age_seconds': age_seconds_from_mtime(DB_PATH),
        'wal_mtime_age_seconds': age_seconds_from_mtime(DB_WAL),
        'dangling_assistant_count': 0,
        'dangling_sessions': [],
        'recent_message_count_5m': 0,
    }
    if not DB_PATH.exists():
        report['error'] = 'opencode.db missing'
        return report
    try:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM message
            WHERE json_extract(data, '$.role') = 'assistant'
              AND json_extract(data, '$.finish') IS NULL
              AND json_extract(data, '$.error') IS NULL
              AND json_extract(data, '$.time.completed') IS NULL
        """)
        report['dangling_assistant_count'] = int(cur.fetchone()[0] or 0)
        cur.execute("""
            SELECT session_id, id, time_created, time_updated
            FROM message
            WHERE json_extract(data, '$.role') = 'assistant'
              AND json_extract(data, '$.finish') IS NULL
              AND json_extract(data, '$.error') IS NULL
              AND json_extract(data, '$.time.completed') IS NULL
            ORDER BY time_updated ASC
            LIMIT 20
        """)
        rows = cur.fetchall()
        now_ts = int(datetime.now().timestamp())
        for row in rows:
            updated = row['time_updated'] or row['time_created']
            age = None
            if updated is not None:
                try:
                    age = max(0, now_ts - int(updated))
                except Exception:
                    age = None
            report['dangling_sessions'].append({
                'session_id': row['session_id'],
                'message_id': row['id'],
                'stale_age_seconds': age,
            })
        cur.execute("SELECT COUNT(*) FROM message WHERE time_updated >= strftime('%s','now') - 300")
        report['recent_message_count_5m'] = int(cur.fetchone()[0] or 0)
        conn.close()
    except Exception as exc:
        report['error'] = str(exc)
    return report


def audit_sqlite_remote() -> dict[str, Any]:
    script = r'''
import json, sqlite3, time
from pathlib import Path
DB_PATH = Path.home()/'.local/share/opencode/opencode.db'
DB_WAL = Path.home()/'.local/share/opencode/opencode.db-wal'
report = {
  'db_exists': DB_PATH.exists(),
  'wal_exists': DB_WAL.exists(),
  'db_mtime_age_seconds': max(0, int(time.time() - DB_PATH.stat().st_mtime)) if DB_PATH.exists() else None,
  'wal_mtime_age_seconds': max(0, int(time.time() - DB_WAL.stat().st_mtime)) if DB_WAL.exists() else None,
  'dangling_assistant_count': 0,
  'dangling_sessions': [],
  'recent_message_count_5m': 0,
}
if not DB_PATH.exists():
  report['error'] = 'opencode.db missing'
  print(json.dumps(report, ensure_ascii=False))
  raise SystemExit(0)
conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
SELECT COUNT(*) FROM message
WHERE json_extract(data, '$.role')='assistant'
  AND json_extract(data, '$.finish') IS NULL
  AND json_extract(data, '$.error') IS NULL
  AND json_extract(data, '$.time.completed') IS NULL
""")
report['dangling_assistant_count'] = int(cur.fetchone()[0] or 0)
cur.execute("""
SELECT session_id, id, time_created, time_updated
FROM message
WHERE json_extract(data, '$.role')='assistant'
  AND json_extract(data, '$.finish') IS NULL
  AND json_extract(data, '$.error') IS NULL
  AND json_extract(data, '$.time.completed') IS NULL
ORDER BY time_updated ASC LIMIT 20
""")
now_ts = int(time.time())
for row in cur.fetchall():
  updated = row['time_updated'] or row['time_created']
  age = None
  if updated is not None:
    try:
      age = max(0, now_ts - int(updated))
    except Exception:
      age = None
  report['dangling_sessions'].append({'session_id': row['session_id'], 'message_id': row['id'], 'stale_age_seconds': age})
cur.execute("SELECT COUNT(*) FROM message WHERE time_updated >= strftime('%s','now') - 300")
report['recent_message_count_5m'] = int(cur.fetchone()[0] or 0)
conn.close()
print(json.dumps(report, ensure_ascii=False))
'''
    return json.loads(run_remote_python(script))


def audit_plugin_state_local() -> dict[str, Any]:
    session_map = read_json(PLUGIN_SESSIONS, {})
    message_state = read_json(PLUGIN_MESSAGES, {})
    continuation = read_json(PLUGIN_CONT, {})
    lines = latest_lines(PLUGIN_DEBUG, limit=400)
    recent_error_lines = [line for line in lines if any(key in line for key in [
        'event.probe.error', 'message.persist.error', 'continuation.remote', 'session.failure.finalized', 'session.error', 'TimeoutError', 'Aborted process'
    ])][-20:]
    retry_empty = sum(1 for line in lines if 'message.persist.retry.empty' in line)
    skip_not_ready = sum(1 for line in lines if 'message.flush.skip.not-ready' in line)
    pending = continuation.get('pending') if isinstance(continuation, dict) else None
    pending_age = None
    if isinstance(pending, dict):
        created_at = parse_ts(pending.get('created_at'))
        if created_at:
            pending_age = int((datetime.now(timezone.utc) - created_at).total_seconds())
    max_message_age = None
    now = datetime.now(timezone.utc)
    if isinstance(message_state, dict):
        ages = []
        for item in message_state.values():
            if not isinstance(item, dict):
                continue
            updated_at = parse_ts(item.get('updatedAt') or item.get('updated_at'))
            if updated_at:
                ages.append(int((now - updated_at).total_seconds()))
        if ages:
            max_message_age = max(ages)
    return {
        'session_map_count': len(session_map) if isinstance(session_map, dict) else 0,
        'message_state_count': len(message_state) if isinstance(message_state, dict) else 0,
        'debug_log_age_seconds': age_seconds_from_mtime(PLUGIN_DEBUG),
        'retry_empty_count_recent': retry_empty,
        'skip_not_ready_count_recent': skip_not_ready,
        'continuation_pending_exists': pending is not None,
        'continuation_pending_age_seconds': pending_age,
        'max_message_state_age_seconds': max_message_age,
        'recent_error_lines': recent_error_lines,
    }


def audit_plugin_state_remote() -> dict[str, Any]:
    script = r'''
import json, time
from datetime import datetime, timezone
from pathlib import Path
BASE = Path.home()/'.cache/session-memory'
PLUGIN_DEBUG = BASE/'plugin-auto-debug.log'
PLUGIN_SESSIONS = BASE/'plugin-auto-sessions.json'
PLUGIN_MESSAGES = BASE/'plugin-auto-message-state.json'
PLUGIN_CONT = BASE/'plugin-auto-continuation.json'

def read_json(path, fallback):
  try:
    return json.loads(path.read_text(encoding='utf-8'))
  except Exception:
    return fallback

def latest_lines(path, limit=400):
  try:
    data = path.read_bytes()
    if len(data) > 512000:
      data = data[-512000:]
    text = data.decode('utf-8', errors='ignore')
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]
  except Exception:
    return []

def parse_ts(value):
  if not value:
    return None
  try:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))
  except Exception:
    return None
session_map = read_json(PLUGIN_SESSIONS, {})
message_state = read_json(PLUGIN_MESSAGES, {})
continuation = read_json(PLUGIN_CONT, {})
lines = latest_lines(PLUGIN_DEBUG, 400)
recent_error_lines = [line for line in lines if any(key in line for key in ['event.probe.error','message.persist.error','continuation.remote','session.failure.finalized','session.error','TimeoutError','Aborted process'])][-20:]
retry_empty = sum(1 for line in lines if 'message.persist.retry.empty' in line)
skip_not_ready = sum(1 for line in lines if 'message.flush.skip.not-ready' in line)
pending = continuation.get('pending') if isinstance(continuation, dict) else None
pending_age = None
if isinstance(pending, dict):
  created_at = parse_ts(pending.get('created_at'))
  if created_at:
    pending_age = int((datetime.now(timezone.utc) - created_at).total_seconds())
max_message_age = None
ages = []
for item in message_state.values() if isinstance(message_state, dict) else []:
  if not isinstance(item, dict):
    continue
  updated_at = parse_ts(item.get('updatedAt') or item.get('updated_at'))
  if updated_at:
    ages.append(int((datetime.now(timezone.utc) - updated_at).total_seconds()))
if ages:
  max_message_age = max(ages)
result = {
  'session_map_count': len(session_map) if isinstance(session_map, dict) else 0,
  'message_state_count': len(message_state) if isinstance(message_state, dict) else 0,
  'debug_log_age_seconds': max(0, int(time.time() - PLUGIN_DEBUG.stat().st_mtime)) if PLUGIN_DEBUG.exists() else None,
  'retry_empty_count_recent': retry_empty,
  'skip_not_ready_count_recent': skip_not_ready,
  'continuation_pending_exists': pending is not None,
  'continuation_pending_age_seconds': pending_age,
  'max_message_state_age_seconds': max_message_age,
  'recent_error_lines': recent_error_lines,
}
print(json.dumps(result, ensure_ascii=False))
'''
    return json.loads(run_remote_python(script))


def audit_logs_local() -> dict[str, Any]:
    desktop_lines = newest_log_lines(DESKTOP_LOGS, limit=300)
    cli_lines = newest_log_lines(CLI_LOG_DIR, limit=300)
    keywords = ['Aborted process', 'failed to load plugin', 'session.error', 'TimeoutError', 'Connection error', 'models.dev']
    desktop_hits = [line for line in desktop_lines if any(k in line for k in keywords)][-20:]
    cli_hits = [line for line in cli_lines if any(k in line for k in keywords)][-20:]
    desktop_age = None
    cli_age = None
    if DESKTOP_LOGS.exists() and any(DESKTOP_LOGS.iterdir()):
        desktop_age = age_seconds_from_mtime(sorted(DESKTOP_LOGS.iterdir(), key=lambda p: p.stat().st_mtime)[-1])
    if CLI_LOG_DIR.exists() and any(CLI_LOG_DIR.iterdir()):
        cli_age = age_seconds_from_mtime(sorted(CLI_LOG_DIR.iterdir(), key=lambda p: p.stat().st_mtime)[-1])
    return {
        'desktop_log_age_seconds': desktop_age,
        'cli_log_age_seconds': cli_age,
        'desktop_error_hits': desktop_hits,
        'cli_error_hits': cli_hits,
    }


def audit_logs_remote() -> dict[str, Any]:
    script = r'''
import json, time
from pathlib import Path
BASE_OPENCODE = Path.home()/'.local/share/opencode/log'
BASE_DESKTOP = Path.home()/'.local/share/ai.opencode.desktop/logs'
keywords = ['Aborted process', 'failed to load plugin', 'session.error', 'TimeoutError', 'Connection error', 'models.dev']

def latest_lines(path, limit=300):
  try:
    data = path.read_bytes()
    if len(data) > 512000:
      data = data[-512000:]
    text = data.decode('utf-8', errors='ignore')
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]
  except Exception:
    return []

def newest(directory):
  if not directory.exists():
    return None
  files = sorted([p for p in directory.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime)
  return files[-1] if files else None
cli_file = newest(BASE_OPENCODE)
desktop_file = newest(BASE_DESKTOP)
cli_lines = latest_lines(cli_file, 300) if cli_file else []
desktop_lines = latest_lines(desktop_file, 300) if desktop_file else []
result = {
  'desktop_log_age_seconds': max(0, int(time.time() - desktop_file.stat().st_mtime)) if desktop_file else None,
  'cli_log_age_seconds': max(0, int(time.time() - cli_file.stat().st_mtime)) if cli_file else None,
  'desktop_error_hits': [line for line in desktop_lines if any(k in line for k in keywords)][-20:],
  'cli_error_hits': [line for line in cli_lines if any(k in line for k in keywords)][-20:],
}
print(json.dumps(result, ensure_ascii=False))
'''
    return json.loads(run_remote_python(script))


def build_issues(sqlite_report: dict[str, Any], plugin_report: dict[str, Any], logs_report: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    status = 'ok'
    issues: list[str] = []
    flagged: list[dict[str, Any]] = []

    sqlite_error = sqlite_report.get('error')
    local_sources_missing = (
        MODE == 'local'
        and sqlite_error == 'opencode.db missing'
        and sqlite_report.get('db_exists') is False
        and plugin_report.get('session_map_count', 0) == 0
        and plugin_report.get('message_state_count', 0) == 0
        and plugin_report.get('debug_log_age_seconds') is None
        and logs_report.get('desktop_log_age_seconds') is None
        and logs_report.get('cli_log_age_seconds') is None
    )
    if local_sources_missing:
        status = 'idle'
    elif sqlite_error:
        status = 'warning'
        issues.append(f"sqlite source issue: {sqlite_error}")
    if plugin_report.get('error'):
        status = 'warning' if status in {'ok', 'idle'} else status
        issues.append(f"plugin state issue: {plugin_report['error']}")
    if logs_report.get('error'):
        status = 'warning' if status in {'ok', 'idle'} else status
        issues.append(f"runtime logs issue: {logs_report['error']}")
    dangling_count = sqlite_report.get('dangling_assistant_count', 0)
    stale_dangling = [s for s in sqlite_report.get('dangling_sessions', []) if (s.get('stale_age_seconds') or 0) > STALE_ASSISTANT_SECONDS]
    if dangling_count > 0:
        status = 'warning' if status == 'ok' else status
        issues.append(f'dangling assistant messages detected: {dangling_count}')
        flagged.extend(stale_dangling or sqlite_report.get('dangling_sessions', [])[:5])
    if stale_dangling:
        status = 'critical'
        issues.append(f'stale dangling assistant messages > {STALE_ASSISTANT_SECONDS}s')

    if (plugin_report.get('debug_log_age_seconds') or 0) > STALE_PLUGIN_SECONDS:
        status = 'warning' if status == 'ok' else status
        issues.append(f'plugin debug log stale > {STALE_PLUGIN_SECONDS}s')
    if plugin_report.get('retry_empty_count_recent', 0) >= 5:
        status = 'warning' if status == 'ok' else status
        issues.append('plugin retry.empty spikes detected')
    if (plugin_report.get('continuation_pending_age_seconds') or 0) > STALE_CONTINUATION_SECONDS:
        status = 'warning' if status != 'critical' else status
        issues.append(f'continuation pending > {STALE_CONTINUATION_SECONDS}s')

    if plugin_report.get('recent_error_lines'):
        status = 'warning' if status == 'ok' else status
        issues.append('plugin recent errors present')
    if logs_report.get('desktop_error_hits') or logs_report.get('cli_error_hits'):
        status = 'warning' if status == 'ok' else status
        issues.append('opencode runtime error lines detected')

    return status, issues, flagged


def _safe_section(name: str, loader) -> dict[str, Any]:
    try:
        return loader()
    except Exception as exc:
        return {'error': str(exc), 'section': name}


def main() -> None:
    sqlite_report = _safe_section('sqlite', lambda: audit_sqlite_remote() if MODE == 'ssh' else audit_sqlite_local())
    plugin_report = _safe_section('plugin_state', lambda: audit_plugin_state_remote() if MODE == 'ssh' else audit_plugin_state_local())
    logs_report = _safe_section('logs', lambda: audit_logs_remote() if MODE == 'ssh' else audit_logs_local())
    status, issues, flagged = build_issues(sqlite_report, plugin_report, logs_report)
    report = {
        'generated_at': iso_now(),
        'status': status,
        'issues': issues,
        'sqlite': sqlite_report,
        'plugin_state': plugin_report,
        'logs': logs_report,
        'flagged_sessions': flagged,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
