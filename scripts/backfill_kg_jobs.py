#!/usr/bin/env python3
import argparse
import asyncio
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update
from sqlalchemy.sql import text
from src.database import async_session, close_db
from src.cache import cache
from src.models import KGJob, Message
from src.services.knowledge_graph import KnowledgeGraphService

WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=0.2)
    return parser.parse_args()


def next_retry_at(now: datetime, reason: str) -> datetime:
    if reason == 'rate_limited_cooldown':
        return now + timedelta(minutes=3)
    if reason == 'rate_limited_upstream':
        return now + timedelta(minutes=5)
    if reason == 'cache_not_connected':
        return now + timedelta(minutes=2)
    if reason == 'db_write_failed':
        return now + timedelta(minutes=8)
    if reason == 'upstream_exception':
        return now + timedelta(minutes=6)
    if reason == 'extract_exception':
        return now + timedelta(minutes=6)
    return now + timedelta(minutes=6)


def should_deadletter(reason: str, attempts: int) -> bool:
    if reason == 'json_extract_failed':
        return attempts >= 5
    if reason in {'rate_limited_cooldown', 'rate_limited_upstream', 'extract_exception', 'cache_not_connected', 'db_write_failed', 'upstream_exception'}:
        return attempts >= 5
    return attempts >= 3


async def main():
    args = parse_args()
    processed = 0
    success = 0
    failed = 0

    await cache.connect()
    try:
        async with async_session() as db:
            now = datetime.now(timezone.utc)

            dead_stmt = update(KGJob).where(
                KGJob.status == 'running',
                KGJob.locked_at < now - timedelta(hours=1),
            ).values(
                status='pending',
                locked_at=None,
                locked_by=None,
                updated_at=now,
            )
            if args.apply:
                await db.execute(dead_stmt)
                await db.commit()

            claim_one_sql = "UPDATE kg_jobs SET status = 'running', locked_by = :worker_id, locked_at = :now, updated_at = :now WHERE id IN (SELECT id FROM kg_jobs WHERE status = 'pending' AND available_at <= :now ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, message_id, attempts"

            preview_jobs = []
            if not args.apply:
                stmt = (
                    select(KGJob.id, KGJob.message_id, KGJob.attempts)
                    .where(KGJob.status == 'pending', KGJob.available_at <= now)
                    .order_by(KGJob.created_at.asc())
                    .limit(args.batch_size)
                )
                result = await db.execute(stmt)
                preview_jobs = list(result.all())
                print(f'pending_jobs={len(preview_jobs)} mode=dry-run worker_id={WORKER_ID}')
                for job_id, _, _ in preview_jobs:
                    print(f'dry_run={job_id}')
                await close_db()
                print(f'total_processed={len(preview_jobs)} total_success=0 total_failed=0')
                return

            print(f'pending_jobs<= {args.batch_size} mode=apply worker_id={WORKER_ID}')
            kg = KnowledgeGraphService(db)
            while processed < args.batch_size:
                now = datetime.now(timezone.utc)
                result = await db.execute(text(claim_one_sql), {
                    'worker_id': WORKER_ID,
                    'now': now,
                })
                row = result.first()
                await db.commit()
                if not row:
                    break
                job_id, message_id, current_attempts = row
                processed += 1
                current_attempts = int(current_attempts or 0)

                msg = await db.get(Message, message_id)
                if not msg:
                    await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                        status='dead_letter',
                        last_error='message_not_found',
                        locked_at=None,
                        locked_by=None,
                        updated_at=datetime.now(timezone.utc),
                    ))
                    await db.commit()
                    failed += 1
                    print(f'deadletter={job_id} reason=message_not_found')
                    continue

                metadata = dict(msg.metadata_json or {})
                result = await kg.extract_knowledge_result(msg)
                attempts = current_attempts + 1
                now = datetime.now(timezone.utc)
                reason = result.get('reason', 'unknown')
                detail = result.get('detail')
                metadata['kg_extract_attempts'] = attempts
                metadata['kg_extract_last_attempt_at'] = now.isoformat()
                metadata['kg_extract_error_detail'] = detail
                metadata['kg_extract_reason'] = reason

                if result.get('ok') or result.get('noop'):
                    await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                        status='succeeded',
                        attempts=attempts,
                        last_error=None,
                        locked_at=None,
                        locked_by=None,
                        updated_at=now,
                    ))
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'done' if result.get('ok') else 'noop'
                    metadata['kg_extract_error'] = None
                    metadata['kg_extract_next_attempt_at'] = None
                    await db.execute(update(Message).where(Message.id == message_id).values(metadata_json=metadata))
                    await db.commit()
                    success += 1
                    print(f"updated={job_id} reason={reason} entities={result.get('entity_count', 0)} relations={result.get('relation_count', 0)}")
                else:
                    failed += 1
                    if reason in {'rate_limited_upstream', 'rate_limited_cooldown'}:
                        print(f'rate_limit_break={job_id} released_remaining=0 reason={reason}')
                        break
                    if should_deadletter(reason, attempts):
                        await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                            status='dead_letter',
                            attempts=attempts,
                            last_error=reason,
                            available_at=now,
                            locked_at=None,
                            locked_by=None,
                            updated_at=now,
                        ))
                        metadata['kg_extract_pending'] = False
                        metadata['kg_extract_status'] = 'deadletter'
                        metadata['kg_extract_error'] = reason
                        metadata['kg_extract_next_attempt_at'] = None
                        print(f'deadletter={job_id} attempts={attempts} reason={reason}')
                    else:
                        retry_at = next_retry_at(now, reason)
                        await db.execute(update(KGJob).where(KGJob.id == job_id).values(
                            status='pending',
                            attempts=attempts,
                            last_error=reason,
                            available_at=retry_at,
                            locked_at=None,
                            locked_by=None,
                            updated_at=now,
                        ))
                        metadata['kg_extract_pending'] = True
                        metadata['kg_extract_status'] = 'retry_wait'
                        metadata['kg_extract_error'] = reason
                        metadata['kg_extract_next_attempt_at'] = retry_at.isoformat()
                        print(f'retry_wait={job_id} attempts={attempts} reason={reason}')
                    await db.execute(update(Message).where(Message.id == message_id).values(metadata_json=metadata))
                    await db.commit()

                await asyncio.sleep(args.sleep_seconds)
    finally:
        await cache.close()
        await close_db()

    print(f'total_processed={processed} total_success={success} total_failed={failed}')


if __name__ == '__main__':
    asyncio.run(main())
