#!/usr/bin/env python3
"""后台补算 embedding 脚本（支持 dry-run / apply）"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from src.database import async_session, close_db
from src.models import Message
from src.embedding_service import create_embedding
from src.metrics import EMBEDDING_BACKFILL_PROCESSED, EMBEDDING_FAIL_COUNT, EMBEDDING_SUCCESS_COUNT


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--apply', action='store_true', help='真正写入 embedding；默认 dry-run')
    return parser.parse_args()


async def main():
    args = parse_args()
    updated = 0
    failed = 0
    async with async_session() as db:
        stmt = (
            select(Message)
            .where(Message.embedding.is_(None), Message.role.in_(['user', 'assistant']))
            .order_by(Message.created_at.asc())
            .limit(args.batch_size)
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())

        mode = 'apply' if args.apply else 'dry-run'
        print(f'pending_messages={len(messages)} mode={mode}')
        for msg in messages:
            if not args.apply:
                EMBEDDING_BACKFILL_PROCESSED.labels(result='dry_run').inc()
                print(f'dry_run={msg.id}')
                continue
            try:
                emb = await create_embedding(msg.content)
                if msg.embedding is None:
                    msg.embedding = emb
                updated += 1
                EMBEDDING_SUCCESS_COUNT.inc()
                EMBEDDING_BACKFILL_PROCESSED.labels(result='updated').inc()
                print(f'updated={msg.id}')
            except Exception as e:
                failed += 1
                EMBEDDING_FAIL_COUNT.inc()
                EMBEDDING_BACKFILL_PROCESSED.labels(result='failed').inc()
                print(f'failed={msg.id} error={e}')
        if args.apply:
            await db.commit()
    await close_db()
    print(f'total_updated={updated} total_failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())
