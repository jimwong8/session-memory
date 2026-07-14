#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Async Worker - Runs in screen, processes pending KG extractions.
Uses V3 (unlimited) instead of R1 (rate-limited).
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models import Message
from src.services.knowledge_graph import KnowledgeGraphService
from src.cache import cache


async def main():
    # Initialize Redis (required by chat_completion)
    try:
        await cache.connect()
        print("Redis cache connected")
    except Exception as e:
        print(f"Redis connection failed: {e}")
        print("Will continue without dynamic model config...")

    # Create fresh engine + session factory bound to THIS event loop
    engine = create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    processed = 0
    success = 0
    failed = 0
    batch_num = 0

    while True:
        batch_num += 1
        async with session_factory() as db:
            kg = KnowledgeGraphService(db)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Fetch batch of pending messages
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
                .limit(10)
            )
            result = await db.execute(stmt)
            messages = list(result.scalars().all())

            if not messages:
                print("No more pending messages. Done!")
                break

            batch_success = 0
            batch_failed = 0

            for msg in messages:
                metadata = dict(msg.metadata_json or {})
                ok = await kg.extract_knowledge(msg)

                attempts = int(msg.metadata_json.get('kg_extract_attempts', 0)) + 1
                metadata['kg_extract_attempts'] = attempts
                metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()

                if ok:
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'done'
                    metadata['kg_extract_error'] = None
                    metadata['kg_extract_next_attempt_at'] = None
                    success += 1
                    batch_success += 1
                else:
                    failed += 1
                    batch_failed += 1
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

                await db.execute(
                    update(Message).where(Message.id == msg.id).values(metadata_json=metadata)
                )
                await db.commit()
                processed += 1

            print(f'Batch {batch_num}: success={batch_success} failed={batch_failed} | Total: processed={processed} success={success} failed={failed}')

            if not messages:
                break

            # 2 seconds between batches
            await asyncio.sleep(2)

    # Close everything cleanly before event loop ends
    try:
        await cache.close()
    except Exception as e:
        print(f"Cache close warning: {e}")
    await engine.dispose()
    print(f'Final: processed={processed} success={success} failed={failed}')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted")
    except Exception as e:
        print(f"Fatal error: {e}")
        raise