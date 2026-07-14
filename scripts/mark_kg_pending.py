#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 CTE 批量标记旧会话消息为 kg_extract_pending（单次 SQL）"""
import asyncio
import argparse
import sys
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import text as sa_text
from src.database import async_session, close_db


async def mark_sessions_for_reprocessing(db, top_n: int = 50, user_id: str = "jimwong"):
    """Single bulk UPDATE via CTE"""
    sql = sa_text("""
        WITH top_sessions AS (
            SELECT id FROM sessions
            WHERE user_id = :user_id
            ORDER BY message_count DESC
            LIMIT :top_n
        )
        UPDATE messages 
        SET metadata_json = jsonb_set(
            COALESCE(metadata_json, '{}'::jsonb),
            '{kg_extract_pending}',
            'true'::jsonb
        )
        WHERE session_id IN (SELECT id FROM top_sessions)
          AND role IN ('user', 'assistant')
          AND length(content) >= 50
        RETURNING session_id, id
    """)
    result = await db.execute(sql.bindparams(user_id=user_id, top_n=top_n))
    rows = result.all()
    await db.commit()

    # Stats per session
    from collections import Counter
    by_session = Counter(r[0] for r in rows)
    print(f"Marked {len(rows)} messages across {len(by_session)} sessions")
    for sid, cnt in by_session.most_common(5):
        print(f"  {str(sid)[:8]}... {cnt} messages")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--top', type=int, default=50)
    parser.add_argument('--user-id', default='jimwong')
    args = parser.parse_args()

    async with async_session() as db:
        await mark_sessions_for_reprocessing(db, args.top, args.user_id)
    await close_db()


if __name__ == '__main__':
    asyncio.run(main())
