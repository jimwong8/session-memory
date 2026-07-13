"""
One-time KG dedup script with periodic commits.
"""

import asyncio
import sys
import os
os.chdir("/app")

from sqlalchemy import text as sa_text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from src.config import settings
from src.models import KGEntity, KGRelation

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def find_duplicates(db: AsyncSession) -> list:
    sql = sa_text("""
        SELECT name, entity_type, COUNT(*) as cnt, array_agg(id ORDER BY updated_at DESC) as ids
        FROM kg_entities
        GROUP BY name, entity_type
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 5000
    """)
    result = await db.execute(sql)
    return result.all()


async def main():
    print("Starting KG dedup (safe, periodic commit)...")
    
    async with async_session() as db:
        dups = await find_duplicates(db)
        total_before = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_entities"))).scalar()
    
    print(f"Found {len(dups)} duplicate groups, {total_before} entities")
    
    total_deleted = 0
    group_count = 0
    
    for row in dups:
        name = row[0]
        ent_type = row[1]
        cnt = row[2]
        ids = row[3]
        
        if len(ids) <= 1:
            continue
        
        keep_id = ids[0]
        remove_ids = ids[1:]
        
        async with async_session() as db:
            for rid in remove_ids:
                await db.execute(
                    update(KGRelation)
                    .where(KGRelation.source_entity_id == rid)
                    .values(source_entity_id=keep_id)
                )
                await db.execute(
                    update(KGRelation)
                    .where(KGRelation.target_entity_id == rid)
                    .values(target_entity_id=keep_id)
                )
                ent = await db.get(KGEntity, rid)
                if ent:
                    await db.delete(ent)
                    total_deleted += 1
            await db.commit()
        
        group_count += 1
        if group_count % 100 == 0:
            print(f"  {group_count}/{len(dups)} groups, deleted {total_deleted}")
    
    print(f"Done! Deleted {total_deleted} duplicate entities")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
