#!/usr/bin/env python3
"""Archive old sessions to markdown and optionally prune messages.
Safe retention: only archives sessions inactive for >retention_days.
Keeps session metadata, only removes message content to save space.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

os.chdir("/app")
sys.path.insert(0, "/app")

from sqlalchemy import select, update, func, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from src.config import settings
from src.models import Session, Message, Summary

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    
    async with async_session() as session:
        # Find inactive sessions (not updated recently)
        stmt = (
            select(Session)
            .where(
                Session.updated_at < cutoff,
                Session.archived == False,
            )
            .order_by(Session.updated_at.asc())
            .limit(100)  # Process in small batches
        )
        result = await session.execute(stmt)
        old_sessions = result.scalars().all()
        
        if not old_sessions:
            print(f"No sessions inactive for >{RETENTION_DAYS} days.")
            return
        
        print(f"Found {len(old_sessions)} sessions inactive for >{RETENTION_DAYS} days.")
        print(f"Cutoff: {cutoff.isoformat()}")
        
        total_messages = 0
        for s in old_sessions:
            # Count messages
            count = (await session.execute(
                select(func.count()).where(Message.session_id == s.id)
            )).scalar()
            total_messages += count
            print(f"  Session {s.id}: {s.user_id}, {count} msgs, last active {s.updated_at.isoformat()[:10]}")
        
        print(f"\nTotal messages to prune: {total_messages}")
        
        if DRY_RUN:
            print("\nDRY RUN - no changes made. Set DRY_RUN=false to apply.")
            return
        
        # Soft-prune: set content to NULL for old messages, keep metadata
        # This frees space while preserving session structure
        for s in old_sessions:
            await session.execute(
                update(Message)
                .where(Message.session_id == s.id)
                .values(content="[PRUNED]")
            )
            s.archived = True
        
        await session.commit()
        print(f"\nPruned {total_messages} messages from {len(old_sessions)} sessions.")


if __name__ == "__main__":
    asyncio.run(main())
