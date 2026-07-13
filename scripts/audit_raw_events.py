#!/usr/bin/env python3
"""raw_events aggregation snapshot"""
from pathlib import Path
import json, os, subprocess

log_dir = Path(os.environ.get("LOG_DIR", "/home/jimwong/session-memory/logs/audits"))
log_dir.mkdir(parents=True, exist_ok=True)

def sql(q):
    r = subprocess.run(
        ["docker", "exec", "session_memory_postgres", "psql", "-U", "postgres", "-d", "central_session", "-At", "-c", q],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

try:
    total = int(sql("SELECT count(*) FROM raw_events") or 0)
    terms_raw = sql("SELECT terminal_id || chr(1) || count(*) FROM raw_events GROUP BY terminal_id ORDER BY count(*) DESC").split("\n")
    types_raw = sql("SELECT event_type || chr(1) || count(*) FROM raw_events GROUP BY event_type ORDER BY count(*) DESC").split("\n")
    sessions_raw = sql("SELECT substring(session_id,1,16) || chr(1) || count(*) FROM raw_events GROUP BY session_id ORDER BY count(*) DESC LIMIT 10").split("\n")
    hours_raw = sql("SELECT to_char(date_trunc('hour',created_at),'YYYY-MM-DD HH24:MI') || chr(1) || count(*) FROM raw_events GROUP BY 1 ORDER BY 1 DESC LIMIT 24").split("\n")
    data = {
        "status": "ok",
        "total_events": total,
        "by_terminal": [dict(zip(["terminal_id","count"], r.split(chr(1)))) for r in terms_raw if r],
        "by_event_type": [dict(zip(["event_type","count"], r.split(chr(1)))) for r in types_raw if r],
        "top_sessions": [dict(zip(["session_id","count"], r.split(chr(1)))) for r in sessions_raw if r],
        "hourly_volume": [dict(zip(["hour","count"], r.split(chr(1)))) for r in hours_raw if r],
        "issues": [],
    }
except Exception as e:
    data = {"status": "error", "error": str(e), "total_events": 0, "by_terminal": [], "by_event_type": [], "top_sessions": [], "hourly_volume": []}

latest = log_dir / "audit_raw_events_latest.json"
latest.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(json.dumps(data, ensure_ascii=False))
