"""
One-time KG noise cleanup.

Removes entities that match noise patterns (generic words).
Uses the kg_governance noise filter.
"""

import asyncio
import sys
import os
os.chdir("/app")

from sqlalchemy import select, delete, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from src.config import settings
from src.models import KGEntity

engine = create_async_engine(settings.database_url, pool_size=3)
async_session = async_sessionmaker(engine, class_=AsyncSession)

# Import the noise filter
from src.services.kg_governance import is_noise_entity


async def main():
    print("Starting KG noise cleanup...")
    
    async with async_session() as db:
        # Load all entities (just id + name + type for memory efficiency)
        rows = (await db.execute(select(KGEntity.id, KGEntity.name, KGEntity.entity_type))).all()
        
        noise_ids = []
        for eid, name, ent_type in rows:
            if is_noise_entity(name or "", ent_type or ""):
                noise_ids.append(eid)
        
        print(f"Total entities: {len(rows)}")
        print(f"Noise entities to remove: {len(noise_ids)}")
        
        if len(sys.argv) > 1 and sys.argv[1] == "--apply":
            # Delete noise entities
            batch_size = 500
            total_deleted = 0
            for i in range(0, len(noise_ids), batch_size):
                batch = noise_ids[i:i + batch_size]
                result = await db.execute(
                    delete(KGEntity).where(KGEntity.id.in_(batch))
                )
                total_deleted += result.rowcount
                print(f"  Deleted batch {i // batch_size + 1}: {result.rowcount}")
            
            await db.commit()
            
            # Count after
            total_after = (await db.execute(sa_text("SELECT COUNT(*) FROM kg_entities"))).scalar()
            print(f"\nCleanup complete!")
            print(f"  Entities remaining: {total_after}")
            print(f"  Removed: {total_deleted}")
        else:
            print("\nDRY RUN — pass --apply to execute")
            print("Sample noise entities to remove:")
            for eid, name, ent_type in rows:
                if is_noise_entity(name or "", ent_type or ""):
                    print(f"  - {name} ({ent_type})")
                    if len([1 for _, n, t in rows if is_noise_entity(n or "", t or "")]) > 10:
                        break
    
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
