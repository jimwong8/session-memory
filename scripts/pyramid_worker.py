#!/usr/bin/env python3
"""金字塔全层 Worker — L1原子 + L2场景 + L3画像 持续循环"""
import asyncio, sys, time, logging
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/app')
from src.database import async_session
from src.cache import cache
from src.services.pyramid.atom_builder import AtomBuilder
from src.services.pyramid.scenario_builder import ScenarioBuilder
from src.services.pyramid.persona_builder import PersonaBuilder
from src.models import Session
from sqlalchemy import select

async def run_l1(db):
    """L1: 处理 5 个未抽取会话"""
    stmt = select(Session).where(
        Session.message_count >= 10,
        Session.pyramid_processed_at.is_(None)
    ).order_by(Session.message_count.desc()).limit(5)
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    if not sessions:
        return 0
    b = AtomBuilder(db)
    count = 0
    for s in sessions:
        try:
            c = await asyncio.wait_for(b.process_session(s), timeout=240)
            count += c
        except Exception:
            pass
    return count

async def run_l2(db):
    """L2: 建场景直到无新材料"""
    b = ScenarioBuilder(db)
    total = 0
    for _ in range(5):
        c = await b.build_for_user('jimwong')
        if not c:
            break
        total += c
    return total

async def run_l3(db):
    """L3: 更新画像"""
    pb = PersonaBuilder(db)
    return 1 if await pb.build_for_user('jimwong') else 0

async def main():
    await cache.connect()
    round_num = 0
    while True:
        round_num += 1
        try:
            async with async_session() as db:
                l1 = await run_l1(db)
                l2 = await run_l2(db)
                l3 = await run_l3(db)
        except Exception as e:
            print(f'R{round_num} ERR: {e}', flush=True)
            l1 = l2 = l3 = 0
        print(f'R{round_num}: L1={l1} L2={l2} L3={l3}', flush=True)
        await asyncio.sleep(300)  # 5分钟后检查新素材

asyncio.run(main())
