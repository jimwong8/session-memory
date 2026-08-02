import re
"""FastAPI 路由 - 会话管理 API"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db, async_session
from src.llm_client import chat_completion, generate_summary as llm_generate_summary
from src.schemas import (
    AdminDashboardResponse,
    CanvasResponse,
    CanvasUpdateRequest,
    ChatRequest,
    ChatResponse,
    ContextStats,
    GlobalModelConfig,
    GlobalModelConfigResponse,
    KGEntityResponse,
    KGGraphNode,
    KGGraphEdge,
    KGGraphResponse,
    KGRelationResponse,
    KGSearchRequest,
    KGSearchResponse,
    KGSessionRefResponse,
    MemoryAtomCreate,
    MemoryAtomResponse,
    MemoryScenarioResponse,
    MessageCreate,
    MessageResponse,
    PersonaResponse,
    PersonaUpdateRequest,
    PluginContinuationState,
    PluginMessageState,
    PluginSessionMapState,
    PluginStateResponse,
    ProjectSharedContextResponse,
    SearchRequest,
    SearchResponse,
    SessionCreate,
    SessionDetail,
    SessionResponse,
    SummaryStatusResponse,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
)
from src.services import ContextBuilder, SessionService, SummaryService, check_and_trigger_summary
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.p3_scrub_export import export_user_memory, import_user_memory
from src.services.pyramid.atom_builder import AtomBuilder
from src.services.pyramid.scenario_builder import ScenarioBuilder
from src.services.pyramid.persona_builder import PersonaBuilder
from src.services.pyramid.canvas_builder import CanvasBuilder
from src.services.recall import hybrid_recall, recall_for_context
from src.tokenizer import count_tokens
from src.models import (
    KGEntity, KGRelation, MemoryAtom, MemoryScenario, Persona,
    Session, Message, Summary, KGJob, OperationHistory,
)
from src.config import settings
from src.cache import cache

router = APIRouter(prefix="/api/v1", tags=["sessions"])

AUDIT_DIR = Path("/app/logs/audits")


# ── 会话 CRUD ────────────────────────────────────


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    data: SessionCreate,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """创建新的会话"""
    svc = SessionService(db)
    session = await svc.create_session(data)
    return SessionResponse.model_validate(session)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    user_id: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    """列出用户的所有会话"""
    svc = SessionService(db)
    sessions = await svc.list_sessions(user_id, limit, offset)
    return [SessionResponse.model_validate(s) for s in sessions]


@router.get("/sessions/recent", response_model=list[SessionResponse])
async def recent_sessions(
    user_id: str | None = Query(None, min_length=1),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    """获取最近活跃的会话（按 updated_at 倒序）"""
    svc = SessionService(db)
    effective_user = user_id or "jimwong"
    sessions = await svc.list_sessions(effective_user, limit=limit, offset=0)
    return [SessionResponse.model_validate(s) for s in sessions]




@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    """获取会话详情（含消息和摘要）"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return SessionDetail(
        id=session.id, user_id=session.user_id, title=session.title,
        visibility=session.visibility, archived=session.archived,
        created_at=session.created_at, updated_at=session.updated_at,
        total_tokens=session.total_tokens, message_count=session.message_count,
        metadata_json=session.metadata_json,
        messages=[MessageResponse.model_validate(m) for m in _],
        summaries=[],
    )


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """删除会话"""
    svc = SessionService(db)
    deleted = await svc.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")


# ── 消息操作 ──────────────────────────────────────


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageResponse,
    status_code=201,
)
async def add_message(
    session_id: uuid.UUID,
    data: MessageCreate,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """手动添加一条消息到会话"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    message = await svc.add_message(session_id, data, generate_embedding=False)

    # 不在请求链路中同步做摘要或 embedding，优先保证消息落库成功
    return MessageResponse.model_validate(message)


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
)
async def get_messages(
    session_id: uuid.UUID,
    count: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[MessageResponse]:
    """获取会话的最近消息"""
    svc = SessionService(db)
    messages = await svc.get_recent_messages(session_id, count)
    return [MessageResponse.model_validate(m) for m in messages]


# ── 聊天（核心）────────────────────────────────────


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
async def chat(
    session_id: uuid.UUID,
    data: ChatRequest,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """
    智能聊天接口

    1. 保存用户消息
    2. 构建智能上下文（摘要 + 向量检索 + 最近消息）
    3. 调用 LLM
    4. 保存 AI 回复
    5. 异步检查摘要触发
    """
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 1. 保存用户消息
    user_msg = MessageCreate(role="user", content=data.message)
    await svc.add_message(session_id, user_msg, generate_embedding=False)

    # 2. 构建上下文
    builder = ContextBuilder(db)
    ctx = await builder.build(
        session_id=session_id,
        user_message=data.message,
        system_prompt=data.system_prompt,
    )
    openai_messages = builder.to_messages(ctx, data.message)

    # 3. 调用 LLM
    reply, tokens_used = await chat_completion(openai_messages)

    # 4. 保存 AI 回复
    assistant_msg = MessageCreate(role="assistant", content=reply)
    await svc.add_message(session_id, assistant_msg, generate_embedding=False)

    return ChatResponse(
        session_id=session_id,
        reply=reply,
        tokens_used=tokens_used,
        context_messages_count=len(ctx.recent_messages),
        summary_included=ctx.summary is not None,
        retrieved_count=len(ctx.retrieved_messages),
    )


# ── 上下文统计 ────────────────────────────────────


@router.get("/sessions/{session_id}/context-stats", response_model=ContextStats)
async def get_context_stats(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ContextStats:
    """获取会话的上下文使用统计（快速估算，不调用 ContextBuilder.build()）"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    summary_svc = SummaryService(db)
    summary = await summary_svc.get_latest_summary(session_id)
    from src.models import Summary as SummaryModel
    from sqlalchemy import func as sa_func
    sum_count_stmt = select(SummaryModel).where(SummaryModel.session_id == session_id).with_only_columns(sa_func.count())
    summaries_count = (await db.execute(sum_count_stmt)).scalar() or 0
    summary_status = await summary_svc.get_summary_status(session_id)

    # Fast estimation: use session.total_tokens directly, no ContextBuilder.build()
    ctx_tokens = session.total_tokens or 0
    budget_ratio = min(ctx_tokens / settings.max_context_tokens, 1.0) if ctx_tokens > 0 else 0.0

    return ContextStats(
        session_id=session_id,
        total_messages=session.message_count,
        total_tokens=session.total_tokens,
        summaries_count=summaries_count,
        context_window_tokens=ctx_tokens,
        context_utilization=budget_ratio,
        budget_state="normal" if budget_ratio < 0.7 else ("warning" if budget_ratio < 0.85 else "critical"),
        budget_ratio=budget_ratio,
        system_prompt_tokens=0,
        user_message_tokens=0,
        summary_enabled=summary_status.get("summary_enabled", False),
        summary_available=summary_status.get("latest_summary_exists", False),
        summary_tokens=summary_status.get("latest_summary_tokens", 0),
        shared_summary_tokens=0,
        graph_hits=0,
        graph_context_tokens=0,
        graph_context_utilization=0.0,
        shared_graph_tokens=0,
        retrieved_tokens=0,
        recent_tokens=0,
        reply_reserve_tokens=0,
    )


# ── 手动触发摘要 ──────────────────────────────────


@router.post("/sessions/{session_id}/summarize", status_code=200)
async def trigger_summary(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动触发摘要生成"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    summary_svc = SummaryService(db)
    summary = await summary_svc.generate_and_save_summary(session_id)

    if summary:
        return {
            "status": "success",
            "summary_id": str(summary.id),
            "tokens": summary.tokens,
        }
    return {"status": "skipped", "reason": "摘要已在生成中或无需生成"}


# ── 消息搜索 ──────────────────────────────────────


@router.post("/sessions/{session_id}/search", response_model=SearchResponse)
async def search_session(
    session_id: uuid.UUID,
    data: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """在会话中搜索消息（混合检索）"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if data.strategy in ("keyword", "vector", "hybrid"):
        hits = await hybrid_recall(
            db,
            data.query,
            strategy=data.strategy,
            session_id=session_id,
            limit=data.limit,
        )
        results = []
        for hit in hits[:data.top_k]:
            stmt = select(Message).where(Message.id == hit.id)
            result = await db.execute(stmt)
            msg = result.scalar_one_or_none()
            if msg:
                results.append(MessageResponse.model_validate(msg))
    else:
        results = await svc.search_similar_messages(
            session_id, data.query, top_k=data.top_k
        )
        results = [MessageResponse.model_validate(m) for m in results]

    return SearchResponse(
        session_id=session_id,
        query=data.query,
        top_k=data.top_k,
        results=results,
    )


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导出会话的完整数据（消息、摘要、KG）"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    kg_svc = KnowledgeGraphService(db)
    entities = await kg_svc.list_entities(session_id=session_id)
    relations = await kg_svc.list_relations(session_id=session_id)

    messages = await svc.get_recent_messages(session_id, count=1000)

    return {
        "session": {
            "id": str(session.id), "user_id": session.user_id, "title": session.title,
            "visibility": session.visibility, "archived": session.archived,
            "created_at": str(session.created_at), "updated_at": str(session.updated_at),
            "total_tokens": session.total_tokens, "message_count": session.message_count,
            "metadata_json": session.metadata_json,
            "messages": [], "summaries": [],
        },
        "messages": [MessageResponse.model_validate(m).model_dump(mode="json") for m in messages],
        "entities": [KGEntityResponse.model_validate(e).model_dump(mode="json") for e in entities],
        "relations": [KGRelationResponse.model_validate(r).model_dump(mode="json") for r in relations],
    }


@router.post("/sessions/{session_id}/archive", status_code=200)
async def archive_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """归档会话"""
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.archived = True
    await db.commit()
    return {"status": "archived", "session_id": str(session_id)}


@router.post("/sessions/{session_id}/unarchive", status_code=200)
async def unarchive_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取消归档会话"""
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.archived = False
    await db.commit()
    return {"status": "unarchived", "session_id": str(session_id)}


@router.get("/sessions/{session_id}/summary-status", response_model=SummaryStatusResponse)
async def get_summary_status(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SummaryStatusResponse:
    """获取会话的摘要状态"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    summary_svc = SummaryService(db)
    status = await summary_svc.get_summary_status(session_id)
    return SummaryStatusResponse(**status)


# ═══════════════════════════════════════════════════════════════
# ── 知识图谱 (KG) ─────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

kg_router = APIRouter(prefix="/api/v1/kg", tags=["kg"])


@kg_router.get("/entities", response_model=list[KGEntityResponse])
async def list_kg_entities(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    """列出所有知识图谱实体"""
    kg_svc = KnowledgeGraphService(db)
    entities = await kg_svc.list_entities(limit=limit)
    return [KGEntityResponse.model_validate(e) for e in entities]


@kg_router.get("/relations", response_model=list[KGRelationResponse])
async def list_kg_relations(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    """列出所有知识图谱关系"""
    kg_svc = KnowledgeGraphService(db)
    relations = await kg_svc.list_relations(limit=limit)
    return [KGRelationResponse.model_validate(r) for r in relations]


@kg_router.get("/graph", response_model=KGGraphResponse)
async def get_kg_graph(
    limit: int = Query(80, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    """获取知识图谱完整结构"""
    kg_svc = KnowledgeGraphService(db)
    nodes, edges = await kg_svc.get_graph(limit=limit)
    return KGGraphResponse(
        node_count=len(nodes),
        edge_count=len(edges),
        nodes=[KGGraphNode(**n) for n in nodes],
        edges=[KGGraphEdge(**e) for e in edges],
    )


@kg_router.post("/search", response_model=KGSearchResponse)
async def search_kg(
    data: KGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> KGSearchResponse:
    """搜索知识图谱"""
    kg_svc = KnowledgeGraphService(db)
    entities, relations = await kg_svc.search(data.query, top_k=data.top_k)
    return KGSearchResponse(
        query=data.query,
        top_k=data.top_k,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )


@kg_router.get("/entities/by-visibility", response_model=list[KGEntityResponse])
async def list_kg_entities_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    """按可见性过滤知识图谱实体"""
    # Join with sessions to filter by visibility
    stmt = (
        select(KGEntity)
        .join(Session, KGEntity.session_id == Session.id)
        .where(Session.visibility == visibility)
        .order_by(KGEntity.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    entities = list(result.scalars().all())
    return [KGEntityResponse.model_validate(e) for e in entities]


@kg_router.get("/relations/by-visibility", response_model=list[KGRelationResponse])
async def list_kg_relations_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    """按可见性过滤知识图谱关系"""
    stmt = (
        select(KGRelation)
        .join(Session, KGRelation.session_id == Session.id)
        .where(Session.visibility == visibility)
        .order_by(KGRelation.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    relations = list(result.scalars().all())
    return [KGRelationResponse.model_validate(r) for r in relations]


@kg_router.get("/graph/by-visibility", response_model=KGGraphResponse)
async def get_kg_graph_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(80, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    """按可见性获取知识图谱结构"""
    # Get sessions with matching visibility
    session_stmt = select(Session.id).where(Session.visibility == visibility)
    session_result = await db.execute(session_stmt)
    session_ids = [row[0] for row in session_result.all()]

    if not session_ids:
        return KGGraphResponse(node_count=0, edge_count=0)

    entity_stmt = (
        select(KGEntity)
        .where(KGEntity.session_id.in_(session_ids))
        .order_by(KGEntity.created_at.desc())
        .limit(limit)
    )
    entity_result = await db.execute(entity_stmt)
    entities = list(entity_result.scalars().all())

    relation_stmt = (
        select(KGRelation)
        .where(KGRelation.session_id.in_(session_ids))
        .limit(limit * 2)
    )
    relation_result = await db.execute(relation_stmt)
    relations = list(relation_result.scalars().all())

    nodes = [
        {"id": str(e.id), "label": e.name, "type": e.entity_type, "session_id": e.session_id}
        for e in entities
    ]
    edges = [
        {
            "id": str(r.id),
            "source": str(r.source_entity_id),
            "target": str(r.target_entity_id),
            "relation_type": r.relation_type,
            "session_id": r.session_id,
        }
        for r in relations
    ]

    return KGGraphResponse(
        node_count=len(nodes),
        edge_count=len(edges),
        nodes=[KGGraphNode(**n) for n in nodes],
        edges=[KGGraphEdge(**e) for e in edges],
    )


@kg_router.get("/entity/{entity_name}/sessions", response_model=list[KGSessionRefResponse])
async def get_entity_sessions(
    entity_name: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGSessionRefResponse]:
    """获取包含指定实体会话列表"""
    stmt = (
        select(KGEntity.session_id, func.count(KGEntity.id).label("entity_count"))
        .where(KGEntity.name == entity_name)
        .group_by(KGEntity.session_id)
        .order_by(func.count(KGEntity.id).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()
    return [
        KGSessionRefResponse(
            session_id=row[0],
            entity_count=row[1],
        )
        for row in rows
    ]


@kg_router.get("/relation/{relation_type}/sessions", response_model=list[KGSessionRefResponse])
async def get_relation_sessions(
    relation_type: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGSessionRefResponse]:
    """获取包含指定关系类型会话列表"""
    stmt = (
        select(KGRelation.session_id, func.count(KGRelation.id).label("relation_count"))
        .where(KGRelation.relation_type == relation_type)
        .group_by(KGRelation.session_id)
        .order_by(func.count(KGRelation.id).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()
    return [
        KGSessionRefResponse(
            session_id=row[0],
            relation_count=row[1],
        )
        for row in rows
    ]


# ── 会话内 KG ─────────────────────────────────────


@router.post("/sessions/{session_id}/kg/search", response_model=KGSearchResponse)
async def search_session_kg(
    session_id: uuid.UUID,
    data: KGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> KGSearchResponse:
    """在会话内搜索知识图谱"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    kg_svc = KnowledgeGraphService(db)
    entities, relations = await kg_svc.search(data.query, session_id=session_id, top_k=data.top_k)
    return KGSearchResponse(
        session_id=session_id,
        query=data.query,
        top_k=data.top_k,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )


@router.get("/sessions/{session_id}/kg/entities", response_model=list[KGEntityResponse])
async def get_session_kg_entities(
    session_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    """获取会话的知识图谱实体"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    kg_svc = KnowledgeGraphService(db)
    entities = await kg_svc.list_entities(session_id=session_id, limit=limit)
    return [KGEntityResponse.model_validate(e) for e in entities]


@router.get("/sessions/{session_id}/kg/relations", response_model=list[KGRelationResponse])
async def get_session_kg_relations(
    session_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    """获取会话的知识图谱关系"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    kg_svc = KnowledgeGraphService(db)
    relations = await kg_svc.list_relations(session_id=session_id, limit=limit)
    return [KGRelationResponse.model_validate(r) for r in relations]


# ═══════════════════════════════════════════════════════════════
# ── 统一查询 ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════


@router.post("/query", response_model=UnifiedQueryResponse)
async def unified_query(
    data: UnifiedQueryRequest,
    db: AsyncSession = Depends(get_db),
) -> UnifiedQueryResponse:
    """统一查询：向量 + 图谱混合检索"""
    messages = []
    entities = []
    relations = []
    decomposed_queries = []
    recency_bias_active = False

    if data.mode in ("vector", "hybrid"):
        hits = await hybrid_recall(
            db,
            data.query,
            strategy="hybrid",
            session_id=data.session_id,
            limit=data.top_k,
        )
        for hit in hits[:data.top_k]:
            stmt = select(Message).where(Message.id == hit.id)
            result = await db.execute(stmt)
            msg = result.scalar_one_or_none()
            if msg:
                messages.append(MessageResponse.model_validate(msg))

    if data.mode in ("graph", "hybrid"):
        kg_svc = KnowledgeGraphService(db)
        ents, rels = await kg_svc.search(
            data.query,
            session_id=data.session_id,
            top_k=data.top_k,
        )
        entities = [KGEntityResponse.model_validate(e) for e in ents]
        relations = [KGRelationResponse.model_validate(r) for r in rels]

    # Temporal recency bias
    recency_weight = 0.15 if recency_bias in ("on", "auto") else 0.0
    if recency_bias == "auto":
        temporal_words = {"latest", "current", "recent", "newest"}
        recency_weight = 0.15 if any(w in data.query.lower() for w in temporal_words) else 0.0
        recency_bias_active = recency_weight > 0
    elif recency_bias == "on":
        recency_bias_active = True

    if recency_weight > 0:
        from src.services.bridge_service import apply_temporal_recency_bias
        apply_temporal_recency_bias(messages, weight=recency_weight)

    return UnifiedQueryResponse(
        mode=data.mode,
        query=data.query,
        session_id=data.session_id,
        top_k=data.top_k,
        messages=messages,
        entities=entities,
        relations=relations,
        decomposed_queries=decomposed_queries,
        recency_bias_active=recency_bias_active,
    )


# ═══════════════════════════════════════════════════════════════
# ── 金字塔记忆 (Pyramid / Memory) ───────────────────────────
# ═══════════════════════════════════════════════════════════════


@router.get("/users/{user_id}/atoms", response_model=list[MemoryAtomResponse])
async def get_user_atoms(
    user_id: str,
    kind: str | None = Query(None, pattern=r"^(preference|decision|fact|constraint|goal|error_pattern|task_state|blocker)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryAtomResponse]:
    """获取用户的记忆原子"""
    stmt = select(MemoryAtom).where(MemoryAtom.user_id == user_id)
    if kind:
        stmt = stmt.where(MemoryAtom.kind == kind)
    stmt = stmt.order_by(MemoryAtom.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    atoms = list(result.scalars().all())
    return [MemoryAtomResponse.model_validate(a) for a in atoms]


@router.get("/users/{user_id}/scenarios", response_model=list[MemoryScenarioResponse])
async def get_user_scenarios(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryScenarioResponse]:
    """获取用户的场景块"""
    stmt = (
        select(MemoryScenario)
        .where(MemoryScenario.user_id == user_id)
        .order_by(MemoryScenario.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    scenarios = list(result.scalars().all())
    return [MemoryScenarioResponse.model_validate(s) for s in scenarios]


@router.get("/users/{user_id}/persona", response_model=PersonaResponse)
async def get_user_persona(
    user_id: str,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """获取用户画像"""
    stmt = select(Persona).where(Persona.user_id == user_id)
    result = await db.execute(stmt)
    persona = result.scalar_one_or_none()
    if not persona:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return PersonaResponse.model_validate(persona)


@router.get("/sessions/{session_id}/atoms", response_model=list[MemoryAtomResponse])
async def get_session_atoms(
    session_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryAtomResponse]:
    """获取会话的记忆原子"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    stmt = (
        select(MemoryAtom)
        .where(MemoryAtom.session_id == session_id)
        .order_by(MemoryAtom.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    atoms = list(result.scalars().all())
    return [MemoryAtomResponse.model_validate(a) for a in atoms]


@router.post("/sessions/{session_id}/pyramid/force-extract", status_code=200)
async def force_extract_pyramid(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """强制从会话提取金字塔记忆"""
    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    builder = AtomBuilder(db)
    count = await builder.process_session(session)
    return {"status": "ok", "atoms_extracted": count, "session_id": str(session_id)}


@router.get("/atoms/{atom_id}", response_model=MemoryAtomResponse)
async def get_atom(
    atom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> MemoryAtomResponse:
    """获取单个记忆原子"""
    stmt = select(MemoryAtom).where(MemoryAtom.id == atom_id)
    result = await db.execute(stmt)
    atom = result.scalar_one_or_none()
    if not atom:
        raise HTTPException(status_code=404, detail="记忆原子不存在")
    return MemoryAtomResponse.model_validate(atom)


@router.get("/users/{user_id}/memory/export")
async def export_memory(user_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Export all memory for a user"""
    from sqlalchemy import select as sa_select
    from src.models import MemoryAtom as MA, MemoryScenario as MS, Persona as PA

    atom_stmt = sa_select(MA).where(MA.user_id == user_id, MA.superseded_by.is_(None)).order_by(MA.created_at.desc()).limit(500)
    atoms = list((await db.execute(atom_stmt)).scalars().all())

    sc_stmt = sa_select(MS).where(MS.user_id == user_id).order_by(MS.created_at.desc()).limit(100)
    scenarios = list((await db.execute(sc_stmt)).scalars().all())

    p_stmt = sa_select(PA).where(PA.user_id == user_id)
    persona = (await db.execute(p_stmt)).scalar_one_or_none()

    return {
        "atoms": [{"id": str(a.id), "kind": a.kind, "content": a.content, "title": a.title} for a in atoms],
        "scenarios": [{"id": str(s.id), "title": s.title, "narrative_md": s.narrative_md} for s in scenarios],
        "personas": [{"user_id": persona.user_id, "profile_md": persona.profile_md} for persona in [persona] if persona],
    }

@router.post("/users/{user_id}/memory/import", status_code=200)
async def import_memory(
    user_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导入用户的记忆数据"""
    count = await import_user_memory(db, user_id, body.data)
    return {"status": "ok", "imported_count": count}


# ═══════════════════════════════════════════════════════════════
# ── 画布 (Canvas) ────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════


@router.get("/sessions/{session_id}/canvas", response_model=CanvasResponse)
async def get_session_canvas(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CanvasResponse:
    """获取会话的 Mermaid 画布"""
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return CanvasResponse(
        session_id=str(session_id),
        canvas_mermaid=session.canvas_mermaid,
    )


@router.put("/sessions/{session_id}/canvas", response_model=CanvasResponse)
async def update_session_canvas(
    session_id: uuid.UUID,
    data: CanvasUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> CanvasResponse:
    """更新会话的 Mermaid 画布"""
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.canvas_mermaid = data.canvas_mermaid
    await db.commit()
    return CanvasResponse(
        session_id=str(session_id),
        canvas_mermaid=session.canvas_mermaid,
    )


@router.post("/sessions/{session_id}/canvas/rebuild", response_model=CanvasResponse)
async def rebuild_session_canvas(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CanvasResponse:
    """重建会话的 Mermaid 画布"""
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    canvas_builder = CanvasBuilder(db)
    mermaid = await canvas_builder.build_for_session(session_id)
    if mermaid:
        session.canvas_mermaid = mermaid
        await db.commit()
    return CanvasResponse(
        session_id=str(session_id),
        canvas_mermaid=session.canvas_mermaid,
    )


# ═══════════════════════════════════════════════════════════════
# ── 插件状态 (Plugin State) ─────────────────────────────────
# ═══════════════════════════════════════════════════════════════

plugin_state_router = APIRouter(prefix="/api/v1/plugin-state", tags=["plugin-state"])


@plugin_state_router.post("/session-map", status_code=200)
async def set_session_map(
    data: PluginSessionMapState,
) -> dict:
    """设置 OpenCode 会话到 Session Memory 的映射"""
    key = f"session-map:{data.opencode_session_id}"
    await cache.set_plugin_state(key, data.model_dump(mode="json"))
    return {"status": "ok", "key": key}


@plugin_state_router.get("/session-map/{opencode_session_id}", response_model=PluginStateResponse)
async def get_session_map(
    opencode_session_id: str,
) -> PluginStateResponse:
    """获取 OpenCode 会话到 Session Memory 的映射"""
    key = f"session-map:{opencode_session_id}"
    data = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=data)


@plugin_state_router.post("/message-state", status_code=200)
async def set_message_state(
    data: PluginMessageState,
) -> dict:
    """设置消息状态"""
    key = f"message-state:{data.session_id}:{data.message_id}"
    await cache.set_plugin_state(key, data.model_dump(mode="json"))
    return {"status": "ok", "key": key}


@plugin_state_router.get("/message-state/{session_id}/{message_id}", response_model=PluginStateResponse)
async def get_message_state(
    session_id: str,
    message_id: str,
) -> PluginStateResponse:
    """获取消息状态"""
    key = f"message-state:{session_id}:{message_id}"
    data = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=data)


@plugin_state_router.post("/continuation", status_code=200)
async def set_continuation_state(
    data: PluginContinuationState,
) -> dict:
    """设置续跑状态"""
    key = f"continuation:{data.source_session_id}"
    await cache.set_plugin_state(key, data.model_dump(mode="json"))
    # Also set as latest
    await cache.set_plugin_state("continuation:latest", data.model_dump(mode="json"))
    return {"status": "ok", "key": key}


@plugin_state_router.get("/continuation/latest", response_model=PluginStateResponse)
async def get_latest_continuation() -> PluginStateResponse:
    """获取最近的续跑状态"""
    key = "continuation:latest"
    data = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=data)


@plugin_state_router.get("/continuation/{source_session_id}", response_model=PluginStateResponse)
async def get_continuation_state(
    source_session_id: str,
) -> PluginStateResponse:
    """获取指定会话的续跑状态"""
    key = f"continuation:{source_session_id}"
    data = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=data)


@plugin_state_router.post("/continuation/{source_session_id}/consume", status_code=200)
async def consume_continuation(
    source_session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """消费续跑状态（标记为已消费）"""
    key = f"continuation:{source_session_id}"
    data = await cache.get_plugin_state(key)
    if not data:
        raise HTTPException(status_code=404, detail="续跑状态不存在")
    data["consumed_at"] = datetime.now(timezone.utc).isoformat()
    await cache.set_plugin_state(key, data)
    return {"status": "consumed", "key": key}


# ═══════════════════════════════════════════════════════════════
# ── 管理接口 (Admin) ────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@admin_router.get("/dashboard")
async def admin_dashboard():
    """获取管理仪表盘聚合数据 (fast)"""
    from src.database import async_session
    from sqlalchemy import text
    async with async_session() as db:
        msgs = (await db.execute(text("SELECT count(*) FROM messages"))).scalar() or 0
        sessions = (await db.execute(text("SELECT count(*) FROM sessions"))).scalar() or 0
        bge = (await db.execute(text("SELECT count(*) FROM messages WHERE embedding IS NOT NULL"))).scalar() or 0
        m3 = (await db.execute(text("SELECT count(*) FROM messages WHERE embedding_m3 IS NOT NULL"))).scalar() or 0
        return {
            "overall_status": "ok",
            "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
            "health": {"status": "ok", "db_counts": {"messages": msgs, "sessions": sessions}},
            "capacity_snapshot": {"total_messages": msgs, "active_sessions": sessions, "embed_pct": round(bge/msgs*100,1), "m3_pct": round(m3/msgs*100,1)},
            "kg_ops": {"status": "ok", "succeeded_total": 0, "pending_total": 0, "running_total": 0, "deadletter_total": 0},
        }


@admin_router.get("/config/model", response_model=GlobalModelConfigResponse)
async def get_model_config() -> GlobalModelConfigResponse:
    """获取全局模型配置"""
    data = await cache.get_plugin_state("global-model-config") or {}
    config = GlobalModelConfig(
        openai_model=data.get("openai_model", settings.openai_model),
        summary_model=data.get("summary_model", settings.summary_model),
        backup_openai_model=data.get("backup_openai_model", settings.backup_openai_model),
        backup_summary_model=data.get("backup_summary_model", settings.backup_summary_model or ""),
        llm_routing_mode=data.get("llm_routing_mode", settings.llm_routing_mode),
    )
    return GlobalModelConfigResponse(value=config)


@admin_router.post("/config/model", status_code=200)
async def set_model_config(
    data: GlobalModelConfig,
) -> dict:
    """更新全局模型配置"""
    await cache.set_plugin_state("global-model-config", data.model_dump(mode="json"))
    return {"status": "ok"}


@admin_router.post("/ops/run-kg-worker-once", status_code=200)
async def run_kg_worker_once(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动运行一次 KG Worker（处理 pending 状态的 KG 任务）"""
    # Fetch pending KG jobs and process them
    stmt = (
        select(KGJob)
        .where(KGJob.status == "pending")
        .order_by(KGJob.created_at.asc())
        .limit(10)
    )
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())

    if not jobs:
        return {"status": "ok", "message": "没有待处理的 KG 任务", "processed": 0}

    kg_svc = KnowledgeGraphService(db)
    processed = 0
    for job in jobs:
        try:
            msg = await db.get(Message, job.message_id)
            if msg:
                extract_result = await kg_svc.extract_knowledge_result(msg)
                if extract_result.get("ok") or extract_result.get("noop"):
                    job.status = "succeeded"
                    processed += 1
                else:
                    job.attempts += 1
                    if job.attempts >= 3:
                        job.status = "deadletter"
                    else:
                        job.status = "retry_wait"
                job.last_error = extract_result.get("reason")
        except Exception as e:
            job.attempts += 1
            job.last_error = str(e)
            if job.attempts >= 3:
                job.status = "deadletter"

    await db.commit()
    return {"status": "ok", "processed": processed, "total_jobs": len(jobs)}


@admin_router.post("/ops/requeue-kg-deadletters", status_code=200)
async def requeue_kg_deadletters(
    reason: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """将死信 KG 任务重新入队"""
    stmt = select(KGJob).where(KGJob.status == "deadletter")
    if reason:
        stmt = stmt.where(KGJob.last_error == reason)
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())

    for job in jobs:
        job.status = "pending"
        job.attempts = 0
        job.last_error = None

    await db.commit()
    return {"status": "ok", "requeued": len(jobs)}


class RepairStuckSessionRequest(BaseModel):
    session_id: str


@admin_router.post("/ops/repair-stuck-session", status_code=200)
async def repair_stuck_session(
    body: RepairStuckSessionRequest,
    apply: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """修复卡住的会话（清理不一致状态）"""
    session_id = uuid.UUID(body.session_id)
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    issues = []

    # Check message count consistency
    actual_count = await db.execute(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    )
    actual = actual_count.scalar() or 0
    if actual != session.message_count:
        issues.append(f"message_count mismatch: stored={session.message_count}, actual={actual}")

    # Check token count consistency
    total_tokens_result = await db.execute(
        select(func.sum(Message.tokens)).where(Message.session_id == session_id)
    )
    total_tok = total_tokens_result.scalar() or 0
    if total_tok != session.total_tokens:
        issues.append(f"total_tokens mismatch: stored={session.total_tokens}, actual={total_tok}")

    if apply:
        session.message_count = actual
        session.total_tokens = total_tok
        await db.commit()

    return {
        "session_id": str(session_id),
        "issues": issues,
        "applied": apply,
    }


@admin_router.post("/metrics/continuation-event", status_code=200)
async def record_continuation_event(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """记录续跑事件指标"""
    body = await request.json()
    from src.metrics import CONTINUATION_TRIGGER_TOTAL, CONTINUATION_LAST_AGE_SECONDS, CONTINUATION_PENDING_GAUGE, CONTINUATION_FAILURE_TOTAL

    event_type = body.get("event", "unknown")
    if event_type == "trigger":
        CONTINUATION_TRIGGER_TOTAL.inc()
    elif event_type == "failure":
        CONTINUATION_FAILURE_TOTAL.inc()
    elif event_type == "pending":
        CONTINUATION_PENDING_GAUGE.set(int(body.get("count", 0)))
    elif event_type == "age":
        CONTINUATION_LAST_AGE_SECONDS.set(int(body.get("seconds", 0)))

    return {"status": "ok", "event": event_type}


@admin_router.post("/ops/reconcile-continuation-metrics", status_code=200)
async def reconcile_continuation_metrics(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """校准续跑指标（从 Redis 同步到 Prometheus）"""
    from src.metrics import CONTINUATION_CONSUMED_TOTAL, CONTINUATION_LAST_AGE_SECONDS, CONTINUATION_PENDING_GAUGE

    latest = await cache.get_plugin_state("continuation:latest")
    consumed_count = 0
    pending_count = 0

    if latest:
        consumed_by = latest.get("consumed_by_session_id")
        if consumed_by:
            consumed_count = 1
            CONTINUATION_CONSUMED_TOTAL.inc()
        else:
            pending_count = 1
            CONTINUATION_PENDING_GAUGE.set(1)

        created_at = latest.get("created_at")
        if created_at:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds()
                CONTINUATION_LAST_AGE_SECONDS.set(age)
            except Exception:
                pass

    return {
        "status": "ok",
        "consumed_count": consumed_count,
        "pending_count": pending_count,
    }


class OpenCodeRuntimeEventPayload(BaseModel):
    source_host: str
    generated_at: str | None = None
    status: str = "unknown"
    issues: list[str] = []
    dangling_assistant_count: int = 0
    flagged_sessions: list[dict] = []
    recent_desktop_errors: int = 0
    recent_cli_errors: int = 0
    recent_plugin_errors: int = 0
    error_classes: dict[str, int] = {}
    raw_audit: dict | None = None


@admin_router.post("/runtime-events/opencode", status_code=200)
async def receive_opencode_runtime_event(
    data: OpenCodeRuntimeEventPayload,
) -> dict:
    """接收 OpenCode 运行时事件"""
    key = f"runtime-events:opencode:{data.source_host}"
    await cache.set_plugin_state(key, data.model_dump(mode="json"))
    # Also store as latest
    await cache.set_plugin_state("runtime-events:opencode:latest", data.model_dump(mode="json"))
    return {"status": "ok", "source_host": data.source_host}


@admin_router.get("/raw-events/stats")
async def get_raw_events_stats() -> dict:
    """获取原始事件统计（直接连接 central_session DB）"""
    try:
        conn = await asyncpg.connect(
            host="postgres",
            port=5432,
            user="postgres",
            password=settings.postgres_password,
            database="central_session",
        )
        try:
            row = await conn.fetchrow("SELECT COUNT(*) as total FROM raw_events")
            total = row["total"] if row else 0

            recent = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM raw_events WHERE created_at > NOW() - INTERVAL '1 hour'"
            )
            recent_count = recent["cnt"] if recent else 0

            event_types = await conn.fetch(
                "SELECT event_type, COUNT(*) as cnt FROM raw_events GROUP BY event_type ORDER BY cnt DESC LIMIT 10"
            )
            types = {r["event_type"]: r["cnt"] for r in event_types}

            return {
                "status": "ok",
                "total_events": total,
                "recent_1h": recent_count,
                "event_types": types,
            }
        finally:
            await conn.close()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# ── 项目 (Projects) ──────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

projects_router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@projects_router.get("/{project_id}/search/kg", response_model=ProjectSharedContextResponse)
async def search_project_kg(
    project_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> ProjectSharedContextResponse:
    """搜索项目范围内的知识图谱"""
    # Find sessions belonging to this project
    stmt = select(Session.id).where(
        func.jsonb_extract_path_text(Session.metadata_json, "project_id") == project_id
    )
    result = await db.execute(stmt)
    session_ids = [row[0] for row in result.all()]

    entities = []
    relations = []
    summaries = []

    if session_ids:
        kg_svc = KnowledgeGraphService(db)
        for sid in session_ids[:20]:  # Limit to 20 sessions
            ents, rels = await kg_svc.search(query, session_id=sid, top_k=top_k)
            entities.extend(ents)
            relations.extend(rels)

        # Get summaries for project sessions
        summary_stmt = (
            select(Summary)
            .where(Summary.session_id.in_(session_ids))
            .order_by(Summary.created_at.desc())
            .limit(5)
        )
        summary_result = await db.execute(summary_stmt)
        summaries = [s.content[:500] for s in summary_result.scalars().all()]

    # Deduplicate
    seen_ents = set()
    unique_entities = []
    for e in entities:
        if e.id not in seen_ents:
            seen_ents.add(e.id)
            unique_entities.append(e)

    seen_rels = set()
    unique_relations = []
    for r in relations:
        if r.id not in seen_rels:
            seen_rels.add(r.id)
            unique_relations.append(r)

    return ProjectSharedContextResponse(
        project_id=project_id,
        summaries=summaries[:5],
        entities=[KGEntityResponse.model_validate(e) for e in unique_entities[:top_k]],
        relations=[KGRelationResponse.model_validate(r) for r in unique_relations[:top_k]],
    )


@projects_router.get("/{project_id}/search/summaries", response_model=ProjectSharedContextResponse)
async def search_project_summaries(
    project_id: str,
    query: str = Query("", min_length=0),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> ProjectSharedContextResponse:
    """搜索项目范围内的摘要"""
    stmt = select(Session.id).where(
        func.jsonb_extract_path_text(Session.metadata_json, "project_id") == project_id
    )
    result = await db.execute(stmt)
    session_ids = [row[0] for row in result.all()]

    summaries = []
    entities = []
    relations = []

    if session_ids:
        summary_stmt = (
            select(Summary)
            .where(Summary.session_id.in_(session_ids))
            .order_by(Summary.created_at.desc())
            .limit(limit)
        )
        if query:
            summary_stmt = summary_stmt.where(Summary.content.ilike(f"%{query}%"))
        summary_result = await db.execute(summary_stmt)
        summaries = [s.content[:500] for s in summary_result.scalars().all()]

        # Also get entities
        kg_svc = KnowledgeGraphService(db)
        entities, relations = await kg_svc.search(query or project_id, top_k=5)

    return ProjectSharedContextResponse(
        project_id=project_id,
        summaries=summaries[:limit],
        entities=[KGEntityResponse.model_validate(e) for e in entities[:5]],
        relations=[KGRelationResponse.model_validate(r) for r in relations[:5]],
    )


# ═══════════════════════════════════════════════════════════════
# MISSING ENDPOINTS — KG, Admin, PluginState, Projects
# ═══════════════════════════════════════════════════════════════

# ── KG endpoints ────────────────────────────────────────────────

@router.get("/kg/entities", response_model=list[KGEntityResponse])
async def list_kg_entities(
    session_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    kg = KnowledgeGraphService(db)
    entities, _ = await kg.search("", session_id=session_id, top_k=limit)
    return [KGEntityResponse.model_validate(e) for e in entities]


@router.get("/kg/relations", response_model=list[KGRelationResponse])
async def list_kg_relations(
    session_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    kg = KnowledgeGraphService(db)
    _, relations = await kg.search("", session_id=session_id, top_k=limit)
    return [KGRelationResponse.model_validate(r) for r in relations]


@router.get("/kg/graph", response_model=KGGraphResponse)
async def get_kg_graph(
    session_id: uuid.UUID | None = Query(None),
    limit: int = Query(80, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    kg = KnowledgeGraphService(db)
    nodes, edges = await kg.get_graph(session_id=session_id, limit=limit)
    return KGGraphResponse(
        session_id=session_id, node_count=len(nodes), edge_count=len(edges),
        nodes=[KGGraphNode(**n) for n in nodes], edges=[KGGraphEdge(**e) for e in edges],
    )


@router.post("/kg/search", response_model=KGSearchResponse)
async def kg_search(
    data: KGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> KGSearchResponse:
    kg = KnowledgeGraphService(db)
    entities, relations = await kg.search(data.query, top_k=data.top_k)
    return KGSearchResponse(
        query=data.query, top_k=data.top_k,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )


@router.get("/kg/entities/by-visibility", response_model=list[KGEntityResponse])
async def list_kg_entities_by_visibility(
    visibility: str = Query(..., pattern="^(team|public)$"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    from sqlalchemy import select as sa_select
    stmt = (
        sa_select(KGEntity)
        .join(Session, KGEntity.session_id == Session.id)
        .where(Session.visibility == visibility)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [KGEntityResponse.model_validate(e) for e in result.scalars().all()]


@router.get("/kg/relations/by-visibility", response_model=list[KGRelationResponse])
async def list_kg_relations_by_visibility(
    visibility: str = Query(..., pattern="^(team|public)$"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    from sqlalchemy import select as sa_select
    stmt = (
        sa_select(KGRelation)
        .join(Session, KGRelation.session_id == Session.id)
        .where(Session.visibility == visibility)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [KGRelationResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/kg/graph/by-visibility", response_model=KGGraphResponse)
async def get_kg_graph_by_visibility(
    visibility: str = Query(..., pattern="^(team|public)$"),
    limit: int = Query(80, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    kg = KnowledgeGraphService(db)
    entities = await kg.list_entities(limit=limit)
    relations = await kg.list_relations(limit=limit * 2)
    # Filter by visibility through session join
    from sqlalchemy import select as sa_select
    ent_stmt = (
        sa_select(KGEntity)
        .join(Session, KGEntity.session_id == Session.id)
        .where(Session.visibility == visibility)
        .limit(limit)
    )
    rel_stmt = (
        sa_select(KGRelation)
        .join(Session, KGRelation.session_id == Session.id)
        .where(Session.visibility == visibility)
        .limit(limit * 2)
    )
    ent_result = await db.execute(ent_stmt)
    rel_result = await db.execute(rel_stmt)
    entities = list(ent_result.scalars().all())
    relations = list(rel_result.scalars().all())
    nodes = [{"id": str(e.id), "label": e.name, "type": e.entity_type, "session_id": str(e.session_id)} for e in entities]
    edges = [{"id": str(r.id), "source": str(r.source_entity_id), "target": str(r.target_entity_id), "relation_type": r.relation_type, "session_id": str(r.session_id)} for r in relations]
    return KGGraphResponse(
        node_count=len(nodes), edge_count=len(edges),
        nodes=[KGGraphNode(**n) for n in nodes], edges=[KGGraphEdge(**e) for e in edges],
    )


@router.get("/kg/entity/{entity_name}/sessions", response_model=list[KGSessionRefResponse])
async def get_sessions_by_entity(
    entity_name: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGSessionRefResponse]:
    kg = KnowledgeGraphService(db)
    entities, _ = await kg.search(entity_name, top_k=limit)
    counts = {}
    for e in entities:
        counts[e.session_id] = counts.get(e.session_id, 0) + 1
    return [KGSessionRefResponse(session_id=sid, entity_count=count) for sid, count in counts.items()]


@router.get("/kg/relation/{relation_type}/sessions", response_model=list[KGSessionRefResponse])
async def get_sessions_by_relation(
    relation_type: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGSessionRefResponse]:
    kg = KnowledgeGraphService(db)
    _, relations = await kg.search(relation_type, top_k=limit)
    counts = {}
    for r in relations:
        counts[r.session_id] = counts.get(r.session_id, 0) + 1
    return [KGSessionRefResponse(session_id=sid, relation_count=count) for sid, count in counts.items()]


# ── Admin endpoints ─────────────────────────────────────────────

AUDIT_DIR = Path(__file__).parent.parent / "logs" / "audits"


@router.get("/admin/dashboard", response_model=AdminDashboardResponse)
async def get_admin_dashboard(db: AsyncSession = Depends(get_db)):
    from src.services.audit_report_service import get_admin_dashboard
    return await get_admin_dashboard()


@router.get("/admin/config/model", response_model=GlobalModelConfigResponse)
async def get_model_config():
    from src.cache import cache
    value = await cache.get_plugin_state("global-model-config")
    if value:
        return GlobalModelConfigResponse(value=GlobalModelConfig(**value))
    from src.config import settings
    return GlobalModelConfigResponse(value=GlobalModelConfig(
        openai_model=settings.openai_model,
        summary_model=settings.summary_model,
        backup_openai_model=settings.backup_openai_model or "",
        backup_summary_model=settings.backup_summary_model or "",
        llm_routing_mode=settings.llm_routing_mode,
    ))


@router.post("/admin/config/model", response_model=GlobalModelConfigResponse)
async def set_model_config(data: GlobalModelConfig):
    from src.cache import cache
    value = data.model_dump()
    await cache.set_plugin_state("global-model-config", value)
    return GlobalModelConfigResponse(value=data)


@router.post("/admin/ops/run-kg-worker-once")
async def run_kg_worker_once(
    batch: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    # Run KG worker script directly
    import asyncio as _asyncio
    proc = await _asyncio.create_subprocess_exec(
        "python", "/app/scripts/backfill_kg.py", "--batch", str(batch), "--apply",
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    result = {"message": stdout.decode()[-500:], "rc": proc.returncode}
    # Record operation
    from src.models import OperationHistory
    from datetime import datetime, timezone
    op = OperationHistory(
        action="run_kg_worker_once",
        target_type="kg_jobs",
        target_id=f"batch={batch}",
        status="ok",
        message=result.get("message", ""),
        operator="api",
    )
    db.add(op)
    await db.commit()
    return result


@router.post("/admin/ops/requeue-kg-deadletters")
async def requeue_kg_deadletters(
    reason: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as sa_text
    if reason:
        sql = sa_text("UPDATE kg_jobs SET status='pending', attempts=0, available_at=now() WHERE status='deadletter' AND last_error LIKE :reason")
        result = await db.execute(sql.bindparams(reason=f"%{reason}%"))
    else:
        sql = sa_text("UPDATE kg_jobs SET status='pending', attempts=0, available_at=now() WHERE status='deadletter'")
        result = await db.execute(sql)
    await db.commit()
    count = result.rowcount
    from src.models import OperationHistory
    op = OperationHistory(
        action="requeue_kg_deadletters", target_type="kg_jobs", target_id=reason or "all",
        status="ok", message=f"requeued={count}", operator="api",
    )
    db.add(op)
    await db.commit()
    return {"requeued": count}


@router.post("/admin/ops/repair-stuck-session")
async def repair_stuck_session(
    session_id: str = Query(...),
    apply: bool = Query(False),
):
    return {
        "session_id": session_id,
        "message": "OpenCode sqlite database is on desktop host. Run repair script locally.",
        "verified_command": f"python3 /home/jimwong/opencode/repair_stuck_opencode_sessions.py --session {session_id}" + (" --apply" if apply else ""),
    }


@router.post("/admin/metrics/continuation-event")
async def record_continuation_metric_event(data: dict) -> dict:
    from src.metrics import CONTINUATION_CONSUMED_TOTAL, CONTINUATION_LAST_AGE_SECONDS, CONTINUATION_PENDING_GAUGE, CONTINUATION_TRIGGER_TOTAL
    event = data.get("event")
    failure_code = data.get("failure_code") or "unknown"
    latest_age_seconds = data.get("latest_age_seconds")
    pending = data.get("pending")
    if event == "stored":
        CONTINUATION_TRIGGER_TOTAL.labels(failure_code=failure_code).inc()
    elif event == "consumed":
        CONTINUATION_CONSUMED_TOTAL.inc()
    if pending is not None:
        try:
            CONTINUATION_PENDING_GAUGE.observe(float(pending))
        except Exception:
            pass
    if latest_age_seconds is not None:
        try:
            CONTINUATION_LAST_AGE_SECONDS.observe(float(latest_age_seconds))
        except Exception:
            pass
    return {"status": "ok", "event": event}


@router.post("/admin/ops/reconcile-continuation-metrics")
async def reconcile_continuation_metrics(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import text as sa_text
    from src.cache import cache
    latest = await cache.get_plugin_state("continuation:latest")
    if not latest:
        return {"reconciled": False, "reason": "no continuation state"}
    consumed_by = latest.get("consumed_by_session_id")
    if consumed_by:
        return {"reconciled": True, "consumed_by": consumed_by}
    return {"reconciled": False, "reason": "not yet consumed"}


@router.post("/admin/runtime-events/opencode")
async def ingest_opencode_runtime_event(data: OpenCodeRuntimeEventPayload) -> PluginStateResponse:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    runtime_payload = data.raw_audit or {
        "status": data.status,
        "issues": data.issues,
        "generated_at": data.generated_at,
        "flagged_sessions": [item.model_dump(mode="json") for item in data.flagged_sessions],
        "sqlite": {"dangling_assistant_count": data.dangling_assistant_count},
        "error_classes": data.error_classes,
    }
    runtime_path = AUDIT_DIR / "audit_opencode_runtime_latest.json"
    runtime_path.write_text(json.dumps(runtime_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    key = f"runtime-summary:{data.source_host}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    await cache.set_plugin_state("runtime-summary:latest", value)
    return PluginStateResponse(key=key, value=value)


@router.get("/admin/raw-events/stats")
async def get_raw_events_stats():
    """Aggregate raw_events from central_session DB via direct PG connection"""
    import asyncpg, os
    pg_host = os.environ.get("POSTGRES_HOST", "session_memory_postgres")
    pg_user = os.environ.get("POSTGRES_USER", "postgres")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "postgres")
    conn = await asyncpg.connect(host=pg_host, port=5432, user=pg_user, password=pg_pass, database="central_session")
    try:
        total = await conn.fetchval("SELECT count(*) FROM raw_events")
        terminals = await conn.fetch("SELECT terminal_id, count(*) as cnt FROM raw_events GROUP BY terminal_id ORDER BY cnt DESC")
        types = await conn.fetch("SELECT event_type, count(*) as cnt FROM raw_events GROUP BY event_type ORDER BY cnt DESC")
        hourly = await conn.fetch("SELECT to_char(date_trunc('hour', created_at), 'YYYY-MM-DD HH24:MI') as hour, count(*) as cnt FROM raw_events GROUP BY 1 ORDER BY 1 DESC LIMIT 24")
        return {
            "total_events": total,
            "by_terminal": [{"terminal_id": r["terminal_id"], "count": r["cnt"]} for r in terminals],
            "by_event_type": [{"event_type": r["event_type"], "count": r["cnt"]} for r in types],
            "hourly_volume": [{"hour": r["hour"], "count": r["cnt"]} for r in hourly],
        }
    finally:
        await conn.close()


# ── Plugin State endpoints ──────────────────────────────────────

@router.post("/plugin-state/session-map", response_model=PluginStateResponse)
async def set_plugin_session_map(data: PluginSessionMapState) -> PluginStateResponse:
    key = f"session-map:{data.opencode_session_id}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/session-map/{opencode_session_id}", response_model=PluginStateResponse)
async def get_plugin_session_map(opencode_session_id: str) -> PluginStateResponse:
    key = f"session-map:{opencode_session_id}"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.post("/plugin-state/message-state", response_model=PluginStateResponse)
async def set_plugin_message_state(data: PluginMessageState) -> PluginStateResponse:
    key = f"message-state:{data.session_id}:{data.message_id}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/message-state/{session_id}/{message_id}", response_model=PluginStateResponse)
async def get_plugin_message_state(session_id: str, message_id: str) -> PluginStateResponse:
    key = f"message-state:{session_id}:{message_id}"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.post("/plugin-state/continuation", response_model=PluginStateResponse)
async def set_plugin_continuation_state(data: PluginContinuationState) -> PluginStateResponse:
    key = f"continuation:{data.source_session_id}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    await cache.set_plugin_state("continuation:latest", value)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/continuation/latest", response_model=PluginStateResponse)
async def get_latest_plugin_continuation_state() -> PluginStateResponse:
    key = "continuation:latest"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/continuation/{source_session_id}", response_model=PluginStateResponse)
async def get_plugin_continuation_state(source_session_id: str) -> PluginStateResponse:
    key = f"continuation:{source_session_id}"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.post("/plugin-state/continuation/{source_session_id}/consume", response_model=PluginStateResponse)
async def consume_plugin_continuation_state(
    source_session_id: str,
    target_session_id: str = Query(..., min_length=1),
) -> PluginStateResponse:
    key = f"continuation:{source_session_id}"
    value = await cache.get_plugin_state(key)
    if value is None:
        return PluginStateResponse(key=key, value=None)
    value = {
        **value,
        "consumed_by_session_id": target_session_id,
        "consumed_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    await cache.set_plugin_state(key, value)
    latest_value = await cache.get_plugin_state("continuation:latest")
    if latest_value and latest_value.get("source_session_id") == source_session_id:
        await cache.set_plugin_state("continuation:latest", value)
    return PluginStateResponse(key=key, value=value)


# ── Project endpoints ───────────────────────────────────────────

@router.get("/projects/{project_id}/search/kg", response_model=ProjectSharedContextResponse)
async def project_search_kg(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ProjectSharedContextResponse:
    from sqlalchemy import select as sa_select
    ent_stmt = (
        sa_select(KGEntity)
        .join(Session, KGEntity.session_id == Session.id)
        .where(Session.metadata_json["project_id"].astext == project_id)
        .limit(limit)
    )
    rel_stmt = (
        sa_select(KGRelation)
        .join(Session, KGRelation.session_id == Session.id)
        .where(Session.metadata_json["project_id"].astext == project_id)
        .limit(limit * 2)
    )
    ent_result = await db.execute(ent_stmt)
    rel_result = await db.execute(rel_stmt)
    entities = [KGEntityResponse.model_validate(e) for e in ent_result.scalars().all()]
    relations = [KGRelationResponse.model_validate(r) for r in rel_result.scalars().all()]
    return ProjectSharedContextResponse(project_id=project_id, entities=entities, relations=relations)


@router.get("/projects/{project_id}/search/summaries")
async def project_search_summaries(
    project_id: str,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select as sa_select
    from src.models import Summary as SummaryModel
    stmt = (
        sa_select(SummaryModel)
        .join(Session, SummaryModel.session_id == Session.id)
        .where(Session.metadata_json["project_id"].astext == project_id)
        .order_by(SummaryModel.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    summaries = result.scalars().all()
    return {
        "project_id": project_id,
        "summaries": [{"content": s.content, "session_id": str(s.session_id), "created_at": str(s.created_at)} for s in summaries],
    }





# -- AutoMem-Inspired Features ------------------------------------

class BridgeSearchRequest(BaseModel):
    seed_message_ids: list[str] = Field(
        description="Seed message IDs to find bridges between",
        min_length=1, max_length=10,
    )
    max_hops: int = Field(default=2, ge=1, le=3)
    per_seed_limit: int = Field(default=5, ge=1, le=20)
    expansion_limit: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class EntityExpansionRequest(BaseModel):
    seed_message_ids: list[str] = Field(
        description="Seed message IDs for entity extraction",
        min_length=0, max_length=10, default=[],
    )
    entity_names: list[str] = Field(
        description="Entity names to expand directly",
        default=[],
    )
    limit_per_entity: int = Field(default=5, ge=1, le=20)
    total_limit: int = Field(default=10, ge=1, le=50)


@router.post("/recall/bridge")
async def bridge_search(
    data: BridgeSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Multi-hop bridge discovery: find memories that connect seeds via graph."""
    from src.services.bridge_service import BridgeService

    svc = BridgeService(db)
    bridges = await svc.discover_bridges(
        data.seed_message_ids,
        max_hops=data.max_hops,
        per_seed_limit=data.per_seed_limit,
        expansion_limit=data.expansion_limit,
        min_score=data.min_score,
    )

    return {
        "status": "success",
        "bridges": [
            {
                "memory_id": b.memory_id,
                "content": b.content,
                "role": b.role,
                "session_id": b.session_id,
                "score": b.score,
                "bridge_path": b.bridge_path,
                "relation_types": b.relation_types,
                "entity_names": b.entity_names,
                "timestamp": b.timestamp,
            }
            for b in bridges
        ],
        "count": len(bridges),
        "seed_count": len(data.seed_message_ids),
        "max_hops": data.max_hops,
    }


@router.post("/recall/expand-entities")
async def entity_expansion(
    data: EntityExpansionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Entity expansion: extract entities from seeds, find related memories."""
    from src.services.bridge_service import BridgeService

    svc = BridgeService(db)
    if data.entity_names:
        expansions = await svc.expand_by_entity_names(
            data.entity_names,
            limit_per_entity=data.limit_per_entity,
            total_limit=data.total_limit,
        )
    else:
        expansions = await svc.expand_by_entity(
            data.seed_message_ids,
            limit_per_entity=data.limit_per_entity,
            total_limit=data.total_limit,
        )

    return {
        "status": "success",
        "expansions": [
            {
                "memory_id": e.memory_id,
                "content": e.content,
                "role": e.role,
                "session_id": e.session_id,
                "score": e.score,
                "matched_entity": e.matched_entity,
                "entity_category": e.entity_category,
                "timestamp": e.timestamp,
            }
            for e in expansions
        ],
        "count": len(expansions),
        "seed_count": len(data.seed_message_ids),
    }


@router.post("/recall/decompose")
async def decompose_query(data: dict):
    """Auto-decompose a complex query into entity+topic sub-queries."""
    from src.services.bridge_service import decompose_query

    query = data.get("query", "")
    decomposed = decompose_query(query)

    words = query.split()
    entities = []
    stopwords = {"what", "would", "could", "does", "did", "how", "why", "when",
                 "where", "which", "who", "will", "can", "should"}
    for i, word in enumerate(words):
        clean = re.sub(r"[^\w]", "", word)
        if len(clean) < 2 or clean.lower() in stopwords:
            continue
        if clean[0].isupper() and clean[1:].islower():
            if i == 0 or (i > 0 and words[i-1][-1] not in ".?!"):
                entities.append(clean)

    topics = [w for w in re.findall(r"[a-z]{4,}", query.lower()) if w not in stopwords][:5]

    return {
        "original_query": query,
        "decomposed_queries": decomposed,
        "entities_found": entities,
        "topics_found": topics,
    }




@router.get("/search/weighted")
async def search_weighted(query: str = Query(...), limit: int = Query(10, ge=1, le=50)):
    """Vector search with length penalty"""
    import psycopg2
    from src.embedding_service import create_embedding
    query_emb = await create_embedding(query)
    conn = psycopg2.connect(host="postgres", dbname="session_memory", user="postgres", password="postgres")
    cur = conn.cursor()
    cur.execute(
        """SELECT m.id, m.content, m.role, m.session_id,
               COALESCE(1.0 - (m.embedding <=> %s::vector), 0.0) AS similarity,
               CASE WHEN length(m.content) > 2000 THEN 0.3 ELSE 1.0 END AS length_penalty
        FROM messages m WHERE m.embedding IS NOT NULL AND m.role IN ('user','assistant')
        ORDER BY m.embedding <=> %s::vector LIMIT %s""",
        (str(query_emb), str(query_emb), limit * 5))
    seen = {}
    results = []
    for row in cur.fetchall():
        sid = str(row[3])
        if sid not in seen:
            seen[sid] = True
            results.append({
                "id": str(row[0]), "content": row[1], "role": row[2],
                "session_id": sid,
                "score": float(row[4]) * float(row[5]),
                "similarity": float(row[4])})
        if len(results) >= limit: break
    conn.close()
    return results


@router.post("/events")
async def ingest_event(data: dict, db: AsyncSession = Depends(get_db)):
    """接收终端钩子事件，写入会话消息"""
    import uuid
    from src.schemas import MessageCreate

    sid = data.get("session_id")
    if not sid:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    try:
        session_uuid = uuid.UUID(sid)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效 session_id")

    svc = SessionService(db)
    session, _ = await svc.get_session(db, session_uuid)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    content = data.get("content", {})
    user_msg = content.get("user", "")
    assistant_msg = content.get("assistant", "")

    results = []
    if user_msg:
        m = await svc.add_message(session_uuid, MessageCreate(role="user", content=user_msg[:2000]), generate_embedding=False)
        results.append(str(m.id))
    if assistant_msg:
        m = await svc.add_message(session_uuid, MessageCreate(role="assistant", content=assistant_msg[:2000]), generate_embedding=False)
        results.append(str(m.id))

    if not results:
        raise HTTPException(status_code=400, detail="消息内容为空")

    return {"status": "ok", "message_ids": results, "count": len(results)}
