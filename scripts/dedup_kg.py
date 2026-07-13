"""
One-time KG dedup script.

Merges entities with same name + entity_type across different sessions.
Keeps the most recently updated entity, redirects relations, removes duplicates.
"""

import asyncio
import sys
from sqlalchemy import text as sa_text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

import os
os.chdir("/app")

from src.config import settings
from src.models import KGEntity, KGRelation

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)


async def find_duplicates(db: AsyncSession) -> list:
    """Find (name, entity_type) pairs that appear more than once."""
    sql = sa_text("""
        SELECT name, entity_type, COUNT(*) as cnt, array_agg(id ORDER BY updated_at DESC) as ids
        FROM kg_entities
        GROUP BY name, entity_type
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """)
    result = await db.execute(sql)
    return result.all()


async def merge_group(db: AsyncSession, name: str, ent_type: str, ids: list) -> int:
    """Merge a group of duplicate entities into one."""
    if len(ids) <= 1:
        return 0
    
    keep_id = ids[0]  # Most recent (sorted by updated_at DESC)
    remove_ids = ids[1:]
    merged = 0
    
    for rid in remove_ids:
        # Redirect relations pointing to the removed entity
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
        # Delete the duplicate
        ent = await db.get(KGEntity, rid)
        if ent:
            await db.delete(ent)
            merged += 1
    
    return merged


async def main():
    print("Starting KG dedup...")
    
    async with async_session() as db:
        # Count before
        total_before = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_entities"))).scalar()
        relations_before = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_relations"))).scalar()
        
        # Find duplicates
        dups = await find_duplicates(db)
        print(f"Found {len(dups)} duplicate groups (same name+type across sessions)")
        print(f"Total entities: {total_before}, relations: {relations_before}")
        
        # Show top 10
        print("\nTop duplicate groups:")
        for row in dups[:10]:
            print(f"  '{row[0]}' ({row[1]}) x{row[2]}")
        
        # Ask for confirmation
        if len(sys.argv) > 1 and sys.argv[1] == "--apply":
            total_merged = 0
            for row in dups:
                name, ent_type, cnt, ids = row
                merged = await merge_group(db, name, ent_type, ids)
                total_merged += merged
            
            await db.commit()
            
            # Count after
            total_after = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_entities"))).scalar()
            relations_after = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_relations"))).scalar()
            
            print(f"\nDedup complete!")
            print(f"  Entities: {total_before} -> {total_after} (removed {total_before - total_after})")
            print(f"  Relations: {relations_before} -> {relations_after}")
        else:
            print("\nDRY RUN — pass --apply to execute")
            print("(No changes made)")
    
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
