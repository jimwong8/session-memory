#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Async Worker - Runs with greenlet support enabled.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Enable greenlet support for SQLAlchemy async
import greenlet
import sqlalchemy
from sqlalchemy.util import greenlet_spawn

# Enable greenlet integration
sqlalchemy.util.greenlet_spawn = greenlet_spawn

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.database import async_session
from src.models import Message
from src.services.knowledge_graph import KnowledgeGraphService
from src.cache import cache


async def main():
    # Initialize Redis
    await cache.connect()
    print("Redis cache connected")

    # Create engine with greenlet support
    engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    processed = 0
    success = 0
    failed = 0

    while True:
        async with session_factory() as db:
            kg = KnowledgeGraphService(db)
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
                .limit(10)
            )
            result = await db.execute(stmt)
            messages = list(result.scalars().all())

            if not messages:
                print("No more pending messages. Done!")
                break

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
                else:
                    failed += 1
                    attempts = int(msg.metadata_json.get('kg_extract_attempts', 0)) + 1
                    if attempts >= 3:
                        metadata['kg_extract_pending'] = False
                        metadata['kg_extract_status'] = 'deadletter'
                        metadata['kg_extract_error'] = 'max attempts'
                        metadata['kg_extract_next_attempt_at'] = None
                    else:
                        metadata['kg_extract_pending'] = True
                        metadata['kg_extract_status'] = 'retry_wait'
                        metadata['kg_extract_error'] = 'extract failed'
                        metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

                await db.execute(
                    update(Message).where(Message.id == msg.id).values(metadata_json=metadata)
                )
                await db.commit()
                processed += 1

            print(f'Batch: processed={len(messages)} success={success} failed={failed}')

            if not messages:
                break

            await asyncio.sleep(1)

    await cache.close()
    print(f'Final: processed={processed} success={success} failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())