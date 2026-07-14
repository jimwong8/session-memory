#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Backfill - Standalone script with proper argument passing.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, update
import argparse

from src.database import async_session
from src.models import Message
from src.services.knowledge_graph import KnowledgeGraphService
from src.cache import cache


async def process_batch(db, kg, batch_size, apply, sleep_seconds):
    """Process one batch of messages. Returns (processed, success, failed)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    
    stmt = (
        select(Message)
        .where(
            Message.role.in_(['user', 'assistant']),
            Message.metadata_json.op('->>')('kg_extract_pending') == 'true',
            (
                func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at').is_(None)
                | (func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at') <= now_iso)
            ),
            func.length(Message.content) >= 50,
        )
        .order_by(Message.created_at.asc())
    )
    
    result = await db.execute(stmt.limit(batch_size))
    messages = list(result.scalars().all())
    
    if not messages:
        return 0, 0, 0
    
    processed = 0
    success = 0
    failed = 0
    
    kg_service = KnowledgeGraphService(db)
    
    for msg in messages:
        processed += 1
        if not apply:
            print(f"dry_run={msg.id}")
            continue
            
        metadata = dict(msg.metadata_json or {})
        ok = await kg_service.extract_knowledge(msg)
        
        attempts = int(msg.metadata_json.get('kg_extract_attempts', 0)) + 1
        metadata['kg_extract_attempts'] = attempts
        metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()
        
        if ok:
            metadata['kg_extract_pending'] = False
            metadata['kg_extract_status'] = 'done'
            metadata['kg_extract_error'] = None
            metadata['kg_extract_next_attempt_at'] = None
            await db.execute(update(Message).where(Message.id == msg.id).values(metadata_json=metadata))
            await db.commit()
            success += 1
            print(f"updated={msg.id}")
        else:
            failed += 1
            attempts_count = int(msg.metadata_json.get('kg_extract_attempts', 0)) + 1
            if attempts_count >= 3:
                metadata['kg_extract_pending'] = False
                metadata['kg_extract_status'] = 'deadletter'
                metadata['kg_extract_error'] = 'max attempts exceeded'
                metadata['kg_extract_next_attempt_at'] = None
                print(f'deadletter={msg.id} attempts={attempts_count}')
            else:
                metadata['kg_extract_pending'] = True
                metadata['kg_extract_status'] = 'retry_wait'
                metadata['kg_extract_error'] = 'extract_knowledge returned false'
                metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                print(f'retry_wait={msg.id}')
            await db.execute(update(Message).where(Message.id == msg.id).values(metadata_json=metadata))
            await db.commit()
    
    return len(messages), success, failed


async def run_backfill(batch_size, apply, sleep_seconds):
    """Main backfill loop."""
    total_processed = 0
    total_success = 0
    total_failed = 0
    
    try:
        while True:
            async with async_session() as db:
                kg = KnowledgeGraphService(db)
                p, s, f = await process_batch(db, kg, batch_size, True, 0)
                print(f'Batch: processed={p} success={s} failed={f}')
                
                if p == 0:
                    print("No more pending messages. Done!")
                    break
            
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
    finally:
        print(f"Final: total_processed={total_processed} success={total_success} failed={total_failed}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=1.5)
    args = parser.parse_args()
    
    asyncio.run(run_backfill(args.batch_size, args.apply, args.sleep_seconds))


if __name__ == '__main__':
    main()