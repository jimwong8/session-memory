#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新处理旧会话的 KG 提取（用新的扩展关系类型 prompt）。

选取消息数最多的top-N会话，删除旧 KG 数据，重新走一遍提取。
对新消息无影响（新消息自动用新 prompt）。

用法:
    python scripts/reprocess_kg.py --top 30 --apply          # 重新处理 top 30 会话
    python scripts/reprocess_kg.py --top 10 --apply --clear # 先删旧 KG 再提取
    python scripts/reprocess_kg.py --top 3 --dry-run        # 只看会处理哪些
"""
import asyncio
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/app') if Path('/app').exists() else Path('/home/jimwong/session-memory')
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func, delete, text as sa_text
from src.database import async_session, close_db
from src.models import Message, KGEntity, KGRelation, Session
from src.services.knowledge_graph import KnowledgeGraphService


async def get_top_sessions(db, top_n: int = 30, user_id: str = "jimwong"):
    """选取消息数最多的 top-N 会话"""
    stmt = (
        select(Session)
        .where(Session.user_id == user_id)
        .order_by(Session.message_count.desc())
        .limit(top_n)
    )
    result = await db.execute(stmt)
    sessions = list(result.scalars().all())
    print(f"Top {len(sessions)} sessions by message_count:")
    for i, s in enumerate(sessions[:10], 1):
        print(f"  {i}. [{s.message_count} msgs] {s.title[:60] or s.id}")
    if len(sessions) > 10:
        print(f"  ... and {len(sessions) - 10} more")
    return sessions


async def clear_session_kg(db, session_id):
    """删除会话的旧 KG 数据"""
    # 获取该会话的所有消息 id
    msg_result = await db.execute(
        select(Message.id).where(Message.session_id == session_id)
    )
    msg_ids = [row[0] for row in msg_result.all()]

    if not msg_ids:
        return 0, 0

    # 删除关联的 kg_relations
    rel_result = await db.execute(
        delete(KGRelation).where(KGRelation.message_id.in_(msg_ids))
    )
    rel_count = rel_result.rowcount

    # 删除 kg_entities (该会话独有的)
    ent_result = await db.execute(
        delete(KGEntity).where(KGEntity.session_id == session_id)
    )
    ent_count = ent_result.rowcount

    await db.commit()
    return ent_count, rel_count


async def reprocess_session(db, session, clear: bool, dry_run: bool):
    """重新处理单个会话的 KG 提取"""
    sid = session.id
    title = (session.title or "")[:50]

    if clear and not dry_run:
        ent_count, rel_count = await clear_session_kg(db, sid)
        print(f"  Cleared: {ent_count} entities, {ent_count} relations removed")

    # 获取需要提取的消息（>=50字符的用户/助手消息）
    msg_stmt = (
        select(Message)
        .where(
            Message.session_id == sid,
            Message.role.in_(['user', 'assistant']),
            func.length(Message.content) >= 50,
        )
        .order_by(Message.created_at.asc())
    )
    result = await db.execute(msg_stmt)
    messages = list(result.scalars().all())

    if dry_run:
        print(f"  [DRY RUN] Would reprocess {len(messages)} messages from: {title}")
        return 0, 0

    kg = KnowledgeGraphService(db)
    success = 0
    failed = 0

    for msg in messages:
        try:
            ok = await kg.extract_knowledge(msg)
            if ok:
                success += 1
            else:
                failed += 1
                print(f"    WARN: extract_knowledge returned False for msg {msg.id}")
        except Exception as exc:
            failed += 1
            print(f"    ERROR: msg {msg.id}: {exc}")
            await db.rollback()

    print(f"  Done: {success} success, {failed} failed | {title}")
    return success, failed


async def main():
    parser = argparse.ArgumentParser(description="Reprocess old sessions with new KG prompt")
    parser.add_argument('--top', type=int, default=30, help='Number of top sessions to process')
    parser.add_argument('--user-id', default='jimwong', help='User ID filter')
    parser.add_argument('--clear', action='store_true', help='Clear old KG data before re-extraction')
    parser.add_argument('--apply', action='store_true', help='Actually perform extraction')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be processed')
    parser.add_argument('--session-id', type=str, help='Process a specific session UUID')
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        print("Use --apply to actually process, or --dry-run to preview.")
        return

    async with async_session() as db:
        if args.session_id:
            # 单会话模式
            session = await db.get(Session, args.session_id)
            if not session:
                print(f"Session {args.session_id} not found")
                return
            sessions = [session]
        else:
            # Top N 会话
            sessions = await get_top_sessions(db, args.top, args.user_id)

        if not sessions:
            print("No sessions found.")
            return

        total_success = 0
        total_failed = 0

        for i, session in enumerate(sessions, 1):
            print(f"\n[{i}/{len(sessions)}] Processing session {session.id} ({session.message_count} msgs)...")
            s, f = await reprocess_session(db, session, args.clear, args.dry_run)
            total_success += s
            total_failed += f

        print(f"\n{'='*60}")
        print(f"Reprocessing complete!")
        print(f"  Sessions: {len(sessions)}")
        print(f"  Total success: {total_success}")
        print(f"  Total failed: {total_failed}")

    await close_db()


if __name__ == '__main__':
    asyncio.run(main())
