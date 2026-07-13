"""金字塔抽取 Worker - L1 原子抽取 + L2 场景聚合 + L3 画像"""

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path("/app") if Path("/app").exists() else Path("/home/jimwong/session-memory")
sys.path.insert(0, str(ROOT))

from src.database import async_session
from src.cache import cache
from src.models import Session, MemoryAtom
from src.services.pyramid.atom_builder import AtomBuilder
from src.services.pyramid.scenario_builder import ScenarioBuilder
from src.services.pyramid.persona_builder import PersonaBuilder
from sqlalchemy import select, func


async def process_atoms(db, session_arg=None):
    """L1 原子抽取"""
    builder = AtomBuilder(db)
    if session_arg:
        stmt = select(Session).where(Session.id == uuid.UUID(session_arg))
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if not session:
            print(f"Session not found: {session_arg}")
            return False
        count = await builder.process_session(session)
        print(f"L1: session {session.id} -> {count} atoms")
        return count > 0
    else:
        session = await builder.pick_session()
        if not session:
            print("L1: no session needs processing")
            return False
        count = await builder.process_session(session)
        print(f"L1: session {session.id} -> {count} atoms")
        return count > 0


async def process_scenarios(db):
    """L2 场景聚合"""
    # 找有原子的所有用户
    stmt = (
        select(MemoryAtom.user_id)
        .where(MemoryAtom.superseded_by.is_(None))
        .distinct()
    )
    result = await db.execute(stmt)
    user_ids = [row[0] for row in result]

    builder = ScenarioBuilder(db)
    total = 0
    for uid in user_ids:
        count = await builder.build_for_user(uid)
        total += count
    if total > 0:
        print(f"L2: built {total} scenarios")
    return total > 0


async def process_personas(db):
    """L3 画像"""
    stmt = (
        select(MemoryAtom.user_id)
        .where(MemoryAtom.superseded_by.is_(None))
        .distinct()
    )
    result = await db.execute(stmt)
    user_ids = [row[0] for row in result]

    builder = PersonaBuilder(db)
    total = 0
    for uid in user_ids:
        ok = await builder.build_for_user(uid)
        if ok:
            total += 1
    if total > 0:
        print(f"L3: built/updated {total} personas")
    return total > 0


async def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    session_arg = sys.argv[2] if len(sys.argv) > 2 else None

    await cache.connect()
    try:
        async with async_session() as db:
            if mode == "atom" or mode == "all":
                await process_atoms(db, session_arg)
            if mode == "scenario" or mode == "all":
                await process_scenarios(db)
            if mode == "persona" or mode == "all":
                await process_personas(db)
    finally:
        await cache.close()


if __name__ == "__main__":
    asyncio.run(main())
