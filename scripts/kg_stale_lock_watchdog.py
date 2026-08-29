#!/usr/bin/env python3
import psycopg2
from datetime import datetime, timezone

DB = dict(host="127.0.0.1", port=5432, dbname="session_memory", user="postgres", password="postgres", connect_timeout=5)
conn = psycopg2.connect(**DB)
conn.autocommit = True
cur = conn.cursor()
cur.execute("""
UPDATE kg_jobs
SET status='pending', locked_at=NULL, locked_by=NULL
WHERE status='running'
  AND (locked_at IS NULL OR locked_at < NOW() - INTERVAL '5 minutes')
""")
reset = cur.rowcount
cur.execute("SELECT status, count(*) FROM kg_jobs GROUP BY status ORDER BY status")
status = dict(cur.fetchall())
cur.execute("SELECT count(*) FROM kg_jobs WHERE status='running' AND locked_at < NOW() - INTERVAL '5 minutes'")
stale = cur.fetchone()[0]
cur.execute("SELECT count(*) FILTER (WHERE embedding IS NOT NULL), count(*) FROM messages")
embedded, total_messages = cur.fetchone()
cur.execute("SELECT count(*) FROM pg_stat_activity")
connections = cur.fetchone()[0]
print(f"reset={reset} stale={stale} pending={status.get('pending',0)} running={status.get('running',0)} completed={status.get('completed',0)} failed={status.get('failed',0)} deadletter={status.get('deadletter',0)} embedding={embedded}/{total_messages} pg_connections={connections}")
cur.close(); conn.close()
