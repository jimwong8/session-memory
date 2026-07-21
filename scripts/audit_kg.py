#!/usr/bin/env python3
"""KG audit - reads DB directly"""
import json, os
from datetime import datetime, timezone
import psycopg2

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("SELECT status, count(*) FROM kg_jobs GROUP BY status")
status_map = {r[0]: r[1] for r in cur.fetchall()}

report = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "status": "ok" if status_map.get("pending", 0) < 1000 else "warning",
    "issues": [],
    "kg_success": status_map.get("completed", 0),
    "kg_fail_json_extract": status_map.get("failed", 0),
    "kg_fail_429": 0,
    "kg_jobs_pending": status_map.get("pending", 0),
    "kg_jobs_deadletter": 0,
}

path = "/app/logs/audits/audit_kg_latest.json"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print("audit_kg: success=" + str(report["kg_success"]) + " pending=" + str(report["kg_jobs_pending"]))
conn.close()
