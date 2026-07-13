#!/usr/bin/env python3
"""Markdown 同步 Worker - 运行一次"""
import asyncio
import sys
from pathlib import Path

ROOT = Path("/app") if Path("/app").exists() else Path("/home/jimwong/session-memory")
sys.path.insert(0, str(ROOT))

from src.database import async_session
from src.cache import cache
from src.services.markdown_sync import sync_user_memory
from src.models import MemoryAtom
from sqlalchemy import select, func


async def main():
    user_id = sys.argv[1] if len(sys.argv) > 1 else "jimwong"
    await cache.connect()
    try:
        async with async_session() as db:
            count = await sync_user_memory(db, user_id)
            print(f"Synced {count} files for user={user_id}")
    finally:
        await cache.close()


if __name__ == "__main__":
    asyncio.run(main())
