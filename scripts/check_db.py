import sys
sys.path.insert(0, '/app')
from src.config import settings
from src.models import Message
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import asyncio

async def check():
    engine = create_async_engine(settings.database_url)
    sess = async_sessionmaker(engine, class_=AsyncSession)
    async with sess() as s:
        total = (await s.execute(select(func.count()).select_from(Message))).scalar()
        with_emb = (await s.execute(select(func.count()).where(Message.embedding.isnot(None)))).scalar()
        pct = round(100*with_emb/total, 1) if total else 0
        print(f'total,total={total}')
        print(f'with_emb,with_emb={with_emb}')
        print(f'pct,pct={pct}')
    await engine.dispose()

asyncio.run(check())
