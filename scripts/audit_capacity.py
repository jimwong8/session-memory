#!/usr/bin/env python3
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def psql(sql: str) -> str:
    return subprocess.check_output([
        'docker', 'exec', 'session_memory_postgres',
        'psql', '-U', 'postgres', '-d', 'session_memory', '-At', '-c', sql,
    ], text=True).strip()


def to_int(value: str) -> int:
    return int(value.strip() or '0')


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    total_messages = to_int(psql("select count(*) from messages;"))
    messages_1h = to_int(psql("select count(*) from messages where created_at >= now() - interval '1 hour';"))
    messages_24h = to_int(psql("select count(*) from messages where created_at >= now() - interval '24 hours';"))
    metadata_pending = to_int(psql("select count(*) from messages where metadata_json->>'kg_extract_pending' = 'true';"))
    metadata_failed = to_int(psql("select count(*) from messages where metadata_json->>'kg_extract_status' = 'failed';"))
    metadata_retry_wait = to_int(psql("select count(*) from messages where metadata_json->>'kg_extract_status' = 'retry_wait';"))
    metadata_deadletter = to_int(psql("select count(*) from messages where metadata_json->>'kg_extract_status' = 'deadletter';"))
    metadata_oldest_pending_age_seconds = to_int(psql("select coalesce(extract(epoch from (now() - min(created_at))),0)::int from messages where metadata_json->>'kg_extract_pending' = 'true';"))
    archived_sessions = to_int(psql("select count(*) from sessions where archived = true;"))
    active_sessions = to_int(psql("select count(*) from sessions where archived = false;"))

    kg_jobs_pending = to_int(psql("select count(*) from kg_jobs where status = 'pending';"))
    kg_jobs_running = to_int(psql("select count(*) from kg_jobs where status = 'running';"))
    kg_jobs_succeeded = to_int(psql("select count(*) from kg_jobs where status = 'succeeded';"))
    kg_jobs_failed = to_int(psql("select count(*) from kg_jobs where status = 'failed';"))
    kg_jobs_deadletter = to_int(psql("select count(*) from kg_jobs where status = 'dead_letter';"))
    kg_jobs_oldest_pending_age_seconds = to_int(psql("select coalesce(extract(epoch from (now() - min(created_at))),0)::int from kg_jobs where status = 'pending';"))

    alerts_path = Path('/home/jimwong/session-memory/logs/alerts/alert_payload_latest.json')
    alert_severity = 'unknown'
    if alerts_path.exists():
        try:
            alert_severity = json.loads(alerts_path.read_text(encoding='utf-8')).get('severity', 'unknown')
        except Exception:
            pass

    status = 'ok'
    issues: list[str] = []
    if kg_jobs_pending > 80:
        status = 'warning'
        issues.append('kg_jobs pending backlog above 80')
    if kg_jobs_deadletter > 0:
        status = 'warning'
        issues.append('kg_jobs deadletter items present')
    if kg_jobs_oldest_pending_age_seconds > 3600:
        status = 'warning'
        issues.append('oldest kg_jobs pending item older than 1 hour')
    if messages_1h > 5000:
        status = 'warning'
        issues.append('message ingress above 5000/hour')
    metadata_counters_differ = metadata_pending != kg_jobs_pending or metadata_deadletter != kg_jobs_deadletter

    report = {
        'generated_at': now,
        'status': status,
        'total_messages': total_messages,
        'messages_last_1h': messages_1h,
        'messages_last_24h': messages_24h,
        'kg_pending_backlog': kg_jobs_pending,
        'active_sessions': active_sessions,
        'archived_sessions': archived_sessions,
        'kg_failed': kg_jobs_failed,
        'kg_retry_wait': 0,
        'kg_deadletter': kg_jobs_deadletter,
        'oldest_pending_age_seconds': kg_jobs_oldest_pending_age_seconds,
        'kg_jobs_pending': kg_jobs_pending,
        'kg_jobs_running': kg_jobs_running,
        'kg_jobs_succeeded': kg_jobs_succeeded,
        'kg_jobs_failed': kg_jobs_failed,
        'kg_jobs_deadletter': kg_jobs_deadletter,
        'kg_jobs_oldest_pending_age_seconds': kg_jobs_oldest_pending_age_seconds,
        'kg_metadata_pending_estimate': metadata_pending,
        'kg_metadata_failed_estimate': metadata_failed,
        'kg_metadata_retry_wait_estimate': metadata_retry_wait,
        'kg_metadata_deadletter_estimate': metadata_deadletter,
        'kg_metadata_oldest_pending_age_seconds': metadata_oldest_pending_age_seconds,
        'kg_counter_source': 'kg_jobs_live',
        'kg_metadata_counter_source': 'messages_metadata_snapshot',
        'kg_metadata_counters_differ': metadata_counters_differ,
        'alert_severity': alert_severity,
        'issues': issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
