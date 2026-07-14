#!/usr/bin/env python3
"""Fix retry_wait messages by removing next_attempt_at"""
import psycopg2

conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/session_memory')
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM messages WHERE metadata_json->>'kg_extract_status' = 'retry_wait'")
retry_count = cur.fetchone()[0]
print(f'Retry wait messages: {retry_count}')

cur.execute("UPDATE messages SET metadata_json = metadata_json - 'kg_extract_next_attempt_at' WHERE metadata_json->>'kg_extract_status' = 'retry_wait'")
conn.commit()

cur.execute("SELECT COUNT(*) FROM messages WHERE metadata_json->>'kg_extract_pending' = 'true'")
pending = cur.fetchone()[0]
print(f'Pending after clear: {pending}')

conn.close()
print('Done')
