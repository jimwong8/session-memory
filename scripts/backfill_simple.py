#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Backfill - Synchronous version using psycopg2 directly.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

import psycopg2
from psycopg2.extras import RealDictCursor

# Use psycopg2 URL format (not asyncpg)
DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"


def get_conn():
    return psycopg2.connect("postgresql://postgres:postgres@postgres:5432/session_memory", cursor_factory=RealDictCursor)


def process_batch(conn, batch_size):
    """Process one batch of messages. Returns (processed, success, failed)."""
    with conn.cursor() as cur:
        # Get pending messages
        cur.execute("""
            SELECT id, content, metadata_json
            FROM messages
            WHERE role IN ('user', 'assistant')
              AND metadata_json->>'kg_extract_pending' = 'true'
              AND (
                  metadata_json->>'kg_extract_next_attempt_at' IS NULL
                  OR metadata_json->>'kg_extract_next_attempt_at' <= %s
              )
              AND length(content) >= 50
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (datetime.now(timezone.utc).isoformat(), batch_size))
        
        messages = cur.fetchall()
        if not messages:
            return 0, 0
        
        processed = 0
        success = 0
        failed = 0
        
        for msg in messages:
            msg_id = msg['id']
            metadata = dict(msg['metadata_json'] or {})
            attempts = int(metadata.get('kg_extract_attempts') or 0) + 1
            metadata['kg_extract_attempts'] = attempts
            metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            
            # For sync backfill, we can't do real KG extraction (needs LLM + async)
            # Just mark as retry_wait for async workers to pick up later
            metadata['kg_extract_pending'] = True
            metadata['kg_extract_status'] = 'retry_wait'
            metadata['kg_extract_error'] = 'queued for async extraction'
            metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            
            cur.execute("""
                UPDATE messages 
                SET metadata_json = %s
                WHERE id = %s
            """, (json.dumps(metadata), msg_id))
            print(f"retry_wait={msg_id} attempts={attempts}")
            failed += 1
        
        conn.commit()
        return len(messages), 0, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=50)
    parser.add_argument('--sleep-seconds', type=float, default=1.0)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    
    conn = psycopg2.connect("postgresql://postgres:postgres@postgres:5432/session_memory", cursor_factory=RealDictCursor)
    conn.autocommit = False
    
    try:
        total_processed = 0
        total_success = 0
        total_failed = 0
        
        while True:
            p, s, f = process_batch(conn, args.batch_size)
            if p == 0:
                print("No more pending messages. Done!")
                break
            print(f"Batch: processed={p} success={s} failed={f}")
            total_failed += f
            
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
        
        print(f"Done. Total failed: {total_failed} (will be processed by async workers)")
        
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()