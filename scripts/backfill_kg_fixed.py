#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone backfill script for KG reprocessing.
Runs outside of the API server context, using proper async execution.
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
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Import models after path setup
from src.models import Message, KGEntity, KGRelation
from src.services.knowledge_graph import KnowledgeGraphService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=1.5)
    parser.add_argument('--limit', type=int, help='Max messages to process (for testing)')
    args = parser.parse_args()

    # Create our own engine/session to avoid greenlet issues
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/session_memory"
    )
    engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=5)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    processed = 0
    success = 0
    failed = 0

    async with async_session() as db:
        kg = KnowledgeGraphService(db)

        now_iso = datetime.now(timezone.utc).isoformat()
        stmt = (
            select(Message)
            .where(
                Message.role.in_(['user', 'assistant']),
                Message.metadata_json.op('->>')('kg_extract_pending') == 'true',
                (
                    (func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at').is_(None))
                    | (func.jsonb_extract_path_text(Message.metadata_json, 'kg_extract_next_attempt_at') <= now_iso)
                )
            )
            .order_by(Message.created_at.asc())
        )
        if args.limit:
            stmt = stmt.limit(args.limit)
        else:
            stmt = stmt.limit(args.batch_size)

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

    await engine.dispose()
    print(f'total_processed={processed} total_success={success} total_failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())