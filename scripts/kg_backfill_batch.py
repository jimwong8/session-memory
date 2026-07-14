#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Backfill Worker - Hybrid: psycopg2 fetch + async batch processing
Avoids MissingGreenlet by running the whole batch in a single event loop.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = "postgresql://postgres:postgres@postgres:5432/session_memory"


def fetch_pending(batch_size):
    """Fetch pending messages via sync psycopg2"""
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, content, session_id, role, metadata_json, created_at
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
            FOR UPDATE OF messages SKIP LOCKED
        """, (now_iso, batch_size))
        messages = cur.fetchall()
    
    conn.close()
    return messages


def save_result(msg_id, metadata):
    """Save result back via sync psycopg2"""
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE messages SET metadata_json = %s WHERE id = %s
        """, (json.dumps(metadata), msg_id))
        conn.commit()
    conn.close()


async def process_batch_async(messages):
    """Process a batch of messages asynchronously with shared resources"""
    sys.path.insert(0, '/app')
    
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@postgres:5432/session_memory",
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    from src.cache import cache
    from src.services.knowledge_graph import KnowledgeGraphService
    from src.models import Message
    
    try:
        await cache.connect()
    except:
        pass
    
    success = 0
    failed = 0
    
    async with session_factory() as db:
        kg = KnowledgeGraphService(db)
        
        for msg in messages:
            message_obj = Message()
            message_obj.id = msg['id']
            message_obj.content = msg['content']
            message_obj.session_id = msg['session_id']
            message_obj.role = msg['role']
            message_obj.metadata_json = msg['metadata_json']
            message_obj.created_at = msg['created_at']
            
            ok = await kg.extract_knowledge(message_obj)
            
            metadata = dict(msg['metadata_json'] or {})
            attempts = int(msg['metadata_json'].get('kg_extract_attempts', 0)) + 1
            metadata['kg_extract_attempts'] = attempts
            metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            
            if ok:
                metadata['kg_extract_pending'] = False
                metadata['kg_extract_status'] = 'done'
                metadata['kg_extract_error'] = None
                metadata['kg_extract_next_attempt_at'] = None
                success += 1
            else:
                failed += 1
                if attempts >= 3:
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'deadletter'
                    metadata['kg_extract_error'] = 'max attempts'
                    metadata['kg_extract_next_attempt_at'] = None
                else:
                    metadata['kg_extract_pending'] = True
                    metadata['kg_extract_status'] = 'retry_wait'
                    metadata['kg_extract_error'] = 'extract failed'
                    metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            
            # Save via sync to avoid nested tx issues
            save_result(msg['id'], metadata)
            
            if (success + failed) % 10 == 0:
                print(f"  progress: success={success} failed={failed}")
    
    try:
        await cache.close()
    except:
        pass
    await engine.dispose()
    
    return len(messages), success, failed


def main():
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    sleep_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    
    total_processed = 0
    total_success = 0
    total_failed = 0
    
    while True:
        # Fetch pending via sync
        messages = fetch_pending(batch_size)
        if not messages:
            break
        
        # Process via async
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        processed, success, failed = loop.run_until_complete(
            process_batch_async(messages)
        )
        loop.close()
        
        total_processed += processed
        total_success += success
        total_failed += failed
        print(f"Batch: processed={processed} success={success} failed={failed} | Total: {total_processed} success={total_success} failed={total_failed}")
        
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    
    print(f"Done. Total: processed={total_processed} success={total_success} failed={total_failed}")


if __name__ == '__main__':
    main()