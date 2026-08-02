#!/usr/bin/env python3
"""金字塔原子抽取 — 多会话并发版 (每会话 V3 抽取 + KAT embedding 走 edgefn 13-key 池)"""
import asyncio, sys, logging, time
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/app')
from src.cache import cache
from src.database import async_session
from src.services.pyramid.atom_builder import AtomBuilder
from sqlalchemy import select
from src.models import Session

CONCURRENCY = 4  # 8 个会话并行 (13 key 够用)
TIMEOUT = 300     # 单会话超时

async def process_one(session, i, total):
    sid = str(session.id)[:8]
    t0 = time.time()
    s = session
    s.pyramid_processed_at = None
    try:
        async with async_session() as db:
            await db.commit()  # 清 processed_at
            b = AtomBuilder(db)
            c = await asyncio.wait_for(b.process_session(s), timeout=TIMEOUT)
        elapsed = time.time() - t0
        print(f'({i+1}/{total}) {sid} atoms={c} {elapsed:.0f}s', flush=True)
        return c > 0
    except asyncio.TimeoutError:
        print(f'({i+1}/{total}) {sid} TIMEOUT {time.time()-t0:.0f}s', flush=True)
        return False
    except Exception as e:
        print(f'({i+1}/{total}) {sid} ERROR {e}', flush=True)
        return False

async def main():
    await cache.connect()
    async with async_session() as db:
        stmt = select(Session).where(
            Session.message_count >= 10,
            Session.pyramid_processed_at.is_(None)
        ).order_by(Session.message_count.desc())
        sessions = (await db.execute(stmt)).scalars().all()
    total = len(sessions)
    print(f'总待处理: {total} 会话, 并发: {CONCURRENCY}', flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def worker(session, i):
        async with sem:
            return await process_one(session, i, total)

    tasks = [worker(s, i) for i, s in enumerate(sessions)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    print(f'DONE total={total} ok={ok}', flush=True)

asyncio.run(main())
