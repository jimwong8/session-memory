#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KG Backfill - Uses SQLAlchemy sync engine directly to avoid greenlet issues.
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, func, update, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from src.config import settings
from src.models import Message, KGEntity, KGRelation
from src.services.knowledge_graph import KnowledgeGraphService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--sleep-seconds', type=float, default=1.5)
    args = parser.parse_args()

    # Create SYNC engine
    sync_url = settings.database_url.replace('postgresql+asyncpg://', 'postgresql://')
    engine = create_engine(
        sync_url,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    Session = sessionmaker(bind=engine)

    processed = 0
    success = 0
    failed = 0

    with Session() as db:
        now_iso = datetime.now(timezone.utc).isoformat()
        
        stmt = text("""
            SELECT id, content, session_id, metadata_json
            FROM messages
            WHERE role IN ('user', 'assistant')
              AND metadata_json->>'kg_extract_pending' = 'true'
              AND (metadata_json->>'kg_extract_next_attempt_at' IS NULL
                   OR metadata_json->>'kg_extract_next_attempt_at' <= :now_iso)
              AND length(content) >= 50
            ORDER BY created_at ASC
            LIMIT :batch_size
        """)
        
        result = db.execute(stmt, {'now_iso': now_iso, 'batch_size': args.batch_size})
        messages = result.mappings().all()
        
        print(f"pending_messages={len(messages)} mode={'apply' if args.apply else 'dry-run'}")
        
        kg = KnowledgeGraphService(db)
        
        for msg in messages:
            processed += 1
            if not args.apply:
                print(f"dry_run={msg['id']}")
                continue
                
            metadata = dict(msg['metadata_json'] or {})
            
            # Run KG extraction in async context
            msg_obj = Message(
                id=msg['id'],
                content=msg['content'],
                session_id=msg['session_id'],
                metadata_json=msg['metadata_json'],
            )
            
            # Run async extraction in event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ok = loop.run_until_complete(kg.extract_knowledge(msg_obj))
            finally:
                loop.close()
                
            attempts = int(metadata.get('kg_extract_attempts') or 0) + 1
            metadata['kg_extract_attempts'] = attempts
            metadata['kg_extract_last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            
            if ok:
                metadata['kg_extract_pending'] = False
                metadata['kg_extract_status'] = 'done'
                metadata['kg_extract_error'] = None
                metadata['kg_extract_next_attempt_at'] = None
                success += 1
                print(f"updated={msg['id']}")
            else:
                failed += 1
                if attempts >= 3:
                    metadata['kg_extract_pending'] = False
                    metadata['kg_extract_status'] = 'deadletter'
                    metadata['kg_extract_error'] = 'extract_knowledge returned false'
                    metadata['kg_extract_next_attempt_at'] = None
                    print(f'deadletter={msg["id"]} attempts={attempts}')
                else:
                    metadata['kg_extract_pending'] = True
                    metadata['kg_extract_status'] = 'retry_wait'
                    metadata['kg_extract_error'] = 'extract_knowledge returned false'
                    metadata['kg_extract_next_attempt_at'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                    print(f'retry_wait={msg["id"]} attempts={attempts}')
            
            # Update metadata
            db.execute(
                update(Message)
                .where(Message.id == msg['id'])
                .values(metadata_json=metadata)
            )
            db.commit()
            
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
        
        print(f'total_processed={processed} total_success={success} total_failed={failed}')

if __name__ == '__main__':
    main()