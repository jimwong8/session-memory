"""
Task-aware proactive prefetch - matches conversation context against
historical KG entities, sessions, and known patterns.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import KGEntity, Session, Message

logger = logging.getLogger(__name__)

TASK_ENTITY_HINTS: dict[str, list[str]] = {
    "pve": ["concept", "function"],
    "network": ["concept", "command", "function"],
    "code": ["file", "function", "error"],
    "research": ["concept", "file"],
    "ops": ["concept", "command", "error"],
    "general": ["concept"],
}

TASK_SKILL_MAP: dict[str, list[str]] = {
    "pve": ["pve-gpu-passthrough", "pve-gpu-llm-deployment", "pve-node-status"],
    "network": ["tailscale-diagnostics", "cloudflare-tunnel-expose", "network-diagnostics"],
    "code": ["systematic-debugging", "test-driven-development", "codebase-archaeology"],
    "research": ["api-reconnaissance", "blogwatcher", "arxiv"],
    "ops": ["debugging-hermes-logs", "session-memory-plugin-debugging", "server-health-check"],
    "general": [],
}

TOKEN_BUDGET = {
    "persona": 800,
    "continuation": 1200,
    "task_context": 1000,
    "recommended_skills": 200,
    "kg_entities": 400,
    "history_excerpts": 600,
}


async def proactive_prefetch(
    db: AsyncSession,
    task_type: str,
    context: str,
    user_id: str = "jimwong",
    top_k: int = 5,
) -> dict[str, Any]:
    """Return task-aware prefetch data: entities, messages, skill recs."""
    context_words = [w for w in context.split() if len(w) >= 2][:5]

    entity_hints = TASK_ENTITY_HINTS.get(task_type, TASK_ENTITY_HINTS["general"])
    ent_params: dict[str, Any] = {}
    ent_conds = []
    for i, w in enumerate(context_words):
        ent_conds.append(f"kg.name ILIKE :ew{i}")
        ent_params[f"ew{i}"] = f"%{w}%"

    entity_type_conds = " OR ".join(
        f"kg.entity_type = \'{t}\'" for t in entity_hints
    )

    if ent_conds:
        ent_where = " OR ".join(ent_conds)
    else:
        ent_where = entity_type_conds if entity_type_conds else "TRUE"

    ent_sql = sa_text(f"""
        SELECT kg.id, kg.name, kg.entity_type, kg.session_id
        FROM kg_entities kg
        WHERE {ent_where}
        ORDER BY kg.created_at DESC
        LIMIT :lim
    """).bindparams(**ent_params, lim=top_k * 4)

    entities: list[dict[str, Any]] = []
    ent_session_ids: list[uuid.UUID] = []
    try:
        ent_result = await db.execute(ent_sql)
        seen_sids = set()
        for row in ent_result.all():
            entities.append({
                "name": row[1],
                "entity_type": row[2],
            })
            sid = uuid.UUID(str(row[3]))
            if sid not in seen_sids:
                seen_sids.add(sid)
                ent_session_ids.append(sid)
    except Exception as e:
        logger.warning("entity prefetch failed: %s", e)

    messages: list[dict[str, Any]] = []
    if ent_session_ids:
        try:
            msg_stmt = (
                select(
                    Message.id,
                    Message.content,
                    Message.role,
                    Message.created_at,
                )
                .where(
                    Message.session_id.in_(ent_session_ids),
                    Message.role.in_(["user", "assistant"]),
                )
                .order_by(Message.created_at.desc())
                .limit(top_k)
            )
            msg_result = await db.execute(msg_stmt)
            for row in msg_result.all():
                content = row[1] or ""
                messages.append({
                    "content": content[:300],
                    "role": row[2],
                    "preview": content[:200],
                })
        except Exception as e:
            logger.warning("message prefetch failed: %s", e)

    recommended_skills = TASK_SKILL_MAP.get(task_type, [])

    latest_sessions: list[dict[str, Any]] = []
    try:
        sess_stmt = (
            select(Session.id, Session.title, Session.updated_at)
            .where(Session.user_id == user_id)
            .order_by(Session.updated_at.desc())
            .limit(5)
        )
        sess_result = await db.execute(sess_stmt)
        latest_sessions = [
            {"title": row[1], "id": str(row[0])} for row in sess_result.all()
        ]
    except Exception:
        pass

    return {
        "task_type": task_type,
        "entities": entities[:top_k],
        "related_messages": messages[:top_k],
        "recommended_skills": recommended_skills[:5],
        "latest_sessions": latest_sessions,
        "token_budget": TOKEN_BUDGET,
    }
