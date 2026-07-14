#!/usr/bin/env python3
import asyncio
from sqlalchemy import text
from src.database import async_session

async def clean():
    async with async_session() as db:
        result = await db.execute(text("""
            DELETE FROM kg_relations 
            WHERE relation_type NOT IN (
                'depends_on','modifies','fixes',
                'exemplifies','prefers_over','contradicts',
                'reinforces','evolved_into','invalidated_by',
                'leads_to','derived_from','part_of',
                'calls','implements','contains','uses','creates',
                'provides','supports','includes','defines','verifies',
                'causes','maps','documents','covers','produces',
                'generates','triggers','executes','indicates',
                'validates','references','configures','is_type_of',
                'persists_to','hosts','connects','exposes'
            )
        """))
        await db.commit()
        print(f'Cleaned {result.rowcount} invalid relations')

asyncio.run(clean())
