"""Proactive prefetch + hybrid search routes."""
from typing import Optional
import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.services.task_router import proactive_prefetch

router = APIRouter(prefix="/api/v1", tags=["proactive"])


class TaskPrefetchRequest(BaseModel):
    task_type: str
    context: str = ""
    user_id: str = "jimwong"
    top_k: int = 5


class TaskPrefetchResponse(BaseModel):
    task_type: str
    entities: list
    related_messages: list
    recommended_skills: list
    latest_sessions: list


class HybridSearchRequest(BaseModel):
    query: str
    user_id: str = "jimwong"
    task_type: str = "general"
    top_k: int = 8


class HybridSearchResponse(BaseModel):
    query: str
    vector_results: list
    entity_results: list
    atoms: list = []
    combined_tokens: int


@router.post("/users/{user_id}/proactive-prefetch", response_model=TaskPrefetchResponse)
async def task_prefetch(
    user_id: str,
    body: TaskPrefetchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Return task-aware prefetch for Hermes memory injection."""
    body.user_id = user_id
    result = await proactive_prefetch(
        db=db,
        task_type=body.task_type,
        context=body.context,
        user_id=user_id,
        top_k=body.top_k,
    )
    return TaskPrefetchResponse(**result)


@router.post("/users/{user_id}/hybrid-search", response_model=HybridSearchResponse)
async def hybrid_search(
    user_id: str,
    body: HybridSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Hybrid search: vector similarity + entity retrieval."""
    from src.embedding_service import create_embedding

    # ⚠ 向量列必须与查询向量的维度一致, 否则维度不匹配 / 静默漏数据:
    #     embedding_nv = 2048 (NIM nemotron, 生产检索已统一到此)
    #     embedding    = 768  (本地 bge-base, 仅作回退)
    #   只查旧列会让迁移后【只有 embedding_nv】的新消息/原子被静默漏掉 (2026-09-22 修复)。
    emb_col = "embedding"
    emb = None
    try:
        from src.services.recall import nim_embed_query
        emb = nim_embed_query(body.query)
    except Exception:
        emb = None
    if emb is not None and len(emb) == 2048:
        emb_col = "embedding_nv"
    else:
        emb = await create_embedding(body.query)
        emb_col = "embedding"
    emb_s = "[" + ",".join(f"{x:.6f}" for x in emb) + "]"

    # Vector search with literal embedding
    vec_sql = sa_text(
        "SELECT id, LEFT(content, 200) as snippet, role, "
        f"{emb_col} <=> '%s'::vector as dist "
        "FROM messages "
        f"WHERE {emb_col} IS NOT NULL AND role IN ('user', 'assistant') "
        "ORDER BY dist LIMIT %d" % (emb_s, body.top_k)
    )
    vec_rows = (await db.execute(vec_sql)).all()
    vector_results = [
        {"id": str(r[0]), "snippet": r[1], "role": r[2], "distance": round(r[3], 4)}
        for r in vec_rows
    ]

    # Entity search: extract alphanumeric "words" from query
    words = re.findall(r'[a-zA-Z0-9_\-\.]+', body.query)
    # Filter short words, take up to 3 longest
    words = sorted([w for w in words if len(w) >= 3], key=len, reverse=True)[:3]

    entity_results = []
    if words:
        # OR-match across all extracted words
        ent_sql = sa_text(
            "SELECT DISTINCT name, entity_type, COUNT(*) as freq "
            "FROM kg_entities "
            "WHERE name ILIKE ANY(:patts) "
            "GROUP BY name, entity_type "
            "ORDER BY freq DESC "
            "LIMIT :k"
        ).bindparams(patts=[f"%{w}%" for w in words], k=body.top_k)
        ent_rows = (await db.execute(ent_sql)).all()
        entity_results = [
            {"name": r[0], "entity_type": r[1], "frequency": r[2]}
            for r in ent_rows
        ]

    # Estimate tokens
    combined_tokens = sum(len(r["snippet"].split()) for r in vector_results)
    combined_tokens += sum(len(r["name"].split()) for r in entity_results)
    combined_tokens = int(combined_tokens * 1.3)

    # 搜 memory_atoms（含 ERL 教训），768维
    atoms = []
    try:
        atom_sql = sa_text(
            "SELECT id, kind, title, LEFT(content, 200) as content, "
            f"{emb_col} <=> '%s'::vector as dist "
            "FROM memory_atoms "
            f"WHERE {emb_col} IS NOT NULL AND superseded_by IS NULL "
            "ORDER BY dist LIMIT %d" % (emb_s, body.top_k)
        )
        atom_rows = (await db.execute(atom_sql)).all()
        atoms = [
            {"id": str(r[0]), "kind": r[1], "title": r[2] or "", "content": r[3], "distance": round(float(r[4]), 4)}
            for r in atom_rows
        ]
    except Exception:
        # ⚠ 吞掉异常后必须 rollback, 否则同一个 session 被毒化,
        #    后续任何 db 查询都会 PendingRollbackError (偶发 500, 极难定位)。
        try:
            await db.rollback()
        except Exception:
            pass

    return HybridSearchResponse(
        query=body.query,
        vector_results=vector_results,
        entity_results=entity_results,
        atoms=atoms,
        combined_tokens=combined_tokens,
    )
