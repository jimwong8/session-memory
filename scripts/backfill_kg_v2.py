#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backfill KG - Uses the existing async_session from src.database to avoid greenlet issues.
"""
import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, update
from src.database import async_session
from src.models import Message
from src.services.knowledge_graph import KnowledgeGraphService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=1.5)
    args = parser.parse_args()

    processed = 0
    success = 0
    failed = 0

    async with async_session() as db:
        now_iso = datetime.now(timezone.utc).isoformat()
        
        stmt = (
            select(Message)
            .where(
                Message.role.in_(['user', 'assistant']),
                Message.metadata_json.op('->>')('kg_extract_pending') == 'true',
                (
                    func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at').is_(None)
                    | (func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at') <= now_iso)
                )
            )
            .order_by(Message.created_at.asc())
            .limit(args.batch_size)
        )
        
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        
        print(f"pending_messages={len(messages)} mode={'apply' if args.apply else 'dry-run'}")
        
        kg = KnowledgeGraphService(db)
        
        for msg in messages:
            processed += 1
            if not args.apply:
                print(f"dry_run={msg.id}")
                continue
                
            metadata = dict(msg.metadata_json or {})
            ok = await kg.extract_knowledge(msg)
            attempts = int(metadata.get('kg_extract_attempts') or 0) + 1
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
                if attempts >= 3:
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'deadletter'
                    metadata['kg_extract_error'] = 'extract_knowledge returned false'
                    metadata['kg_extract_next_attempt_at'] = None
                    print(f'deadletter={msg.id} attempts={attempts}')
                else:
                    metadata['kg_extract_pending'] = True
                    metadata['kg_extract_status'] = 'retry_wait'
                    metadata['kg_extract_error'] = 'extract_knowledge returned false'
                    metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                    print(f'retry_wait={msg.id} attempts={attempts}')
                await db.execute(update(Message).where(Message.id == msg.id).values(metadata_json=metadata))
                await db.commit()
            
            if args.sleep_seconds > 0:
                await asyncio.sleep(args.sleep_seconds)

    print(f'total_processed={processed} total_success={success} total_failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())