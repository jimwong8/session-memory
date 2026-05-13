"""FastAPI 路由 - 会话管理 API"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.utils.compress_search_results import compress_search_result
from src.cache import cache
from src.config import settings
from src.database import get_db
from src.llm_client import chat_completion
from src.schemas import (
    ChatRequest,
    ChatResponse,
    ContextStats,
    KGEntityResponse,
    KGRelationResponse,
    KGSearchRequest,
    KGSearchResponse,
    KGSessionRefResponse,
    KGGraphResponse,
    UnifiedQueryRequest,
    UnifiedQueryResponse,
    MessageCreate,
    MessageResponse,
    PluginContinuationState,
    PluginMessageState,
    PluginSessionMapState,
    PluginStateResponse,
    SearchRequest,
    SearchResponse,
    SessionCreate,
    SessionDetail,
    SessionResponse,
    SummaryStatusResponse,
    ProjectSharedContextResponse,
    AdminDashboardResponse,
    AuditHealthSummary,
    AuditContinuationSummary,
    AuditDuplicatesSummary,
    AuditKGSummary,
    AuditCapacitySummary,
    AuditAlertsSummary,
    AuditTrendsSummary,
    AuditSharedMemorySummary,
    GlobalModelConfig,
    GlobalModelConfigResponse,
    OpenCodeRuntimeEventPayload,
)
from src.services import ContextBuilder, SessionService, SummaryService, check_and_trigger_summary
from src.services.knowledge_graph import KnowledgeGraphService
from src.tokenizer import count_tokens
from src.models import KGEntity, KGRelation, Session, OperationHistory
from pathlib import Path
import json

AUDIT_DIR = Path("/app/logs/audits")
ALERT_DIR = Path("/app/logs/alerts")
router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/admin/config/model", response_model=GlobalModelConfigResponse)
async def get_global_model_config() -> GlobalModelConfigResponse:
    value = await cache.get_plugin_state("global-model-config") or {
        "openai_model": settings.openai_model,
        "summary_model": settings.summary_model,
        "backup_openai_model": settings.backup_openai_model,
        "backup_summary_model": settings.backup_summary_model,
        "llm_routing_mode": settings.llm_routing_mode,
    }
    value.setdefault("backup_openai_model", settings.backup_openai_model)
    value.setdefault("backup_summary_model", settings.backup_summary_model)
    value.setdefault("llm_routing_mode", settings.llm_routing_mode)
    return GlobalModelConfigResponse(value=GlobalModelConfig(**value))


@router.post("/admin/config/model", response_model=GlobalModelConfigResponse)
async def set_global_model_config(data: GlobalModelConfig) -> GlobalModelConfigResponse:
    value = data.model_dump()
    await cache.set_plugin_state("global-model-config", value)
    return GlobalModelConfigResponse(value=GlobalModelConfig(**value))


async def _record_operation_history(db: AsyncSession, *, action: str, target_type: str | None = None, target_id: str | None = None, status: str = "ok", message: str | None = None, operator: str = "jimwong") -> None:
    item = OperationHistory(action=action, target_type=target_type, target_id=target_id, status=status, message=message, operator=operator)
    db.add(item)
    await db.commit()



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
    visibility: str | None = Query(None, pattern=r"^(private|team|public)$"),
    archived: bool | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    """列出用户的所有会话"""
    svc = SessionService(db)
    sessions = await svc.list_sessions(user_id, limit, offset)
    if visibility:
        sessions = [s for s in sessions if getattr(s, "visibility", "private") == visibility]
    if archived is not None:
        sessions = [s for s in sessions if getattr(s, "archived", False) == archived]
    return [SessionResponse.model_validate(s) for s in sessions]


@router.get("/sessions/recent", response_model=list[SessionResponse])
async def list_recent_sessions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    visibility: str | None = Query(None, pattern=r"^(private|team|public)$"),
    archived: bool | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    stmt = select(Session).order_by(Session.updated_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    sessions = list(result.scalars().all())
    if visibility:
        sessions = [s for s in sessions if getattr(s, 'visibility', 'private') == visibility]
    if archived is not None:
        sessions = [s for s in sessions if getattr(s, 'archived', False) == archived]
    return [SessionResponse.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    """获取会话详情（含消息和摘要）"""
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return SessionDetail.model_validate(session)


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: uuid.UUID,
    format: str = Query("json", pattern=r"^(json|markdown)$"),
    db: AsyncSession = Depends(get_db),
):
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    detail = SessionDetail.model_validate(session)
    if format == "json":
        return detail.model_dump(mode="json")

    lines = [f"# {detail.title or 'untitled'}", ""]
    lines.append(f"- session_id: {detail.id}")
    lines.append(f"- user_id: {detail.user_id}")
    lines.append(f"- visibility: {detail.visibility}")
    lines.append(f"- total_tokens: {detail.total_tokens}")
    lines.append(f"- message_count: {detail.message_count}")
    lines.append("")

    if detail.summaries:
        lines.append("## Summaries")
        for s in detail.summaries:
            lines.append(f"- [{s.created_at}] tokens={s.tokens} range={s.message_range_start}-{s.message_range_end}")
            lines.append(f"  {s.content}")
        lines.append("")

    lines.append("## Messages")
    for m in detail.messages:
        lines.append(f"### {m.role} · {m.created_at}")
        lines.append(m.content)
        lines.append("")

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@router.post("/sessions/{session_id}/archive")
async def archive_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.archived = True
    await db.commit()
    await db.refresh(session)
    return {"status": "success", "session_id": str(session.id), "archived": session.archived}


@router.post("/sessions/{session_id}/unarchive")
async def unarchive_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.archived = False
    await db.commit()
    await db.refresh(session)
    return {"status": "success", "session_id": str(session.id), "archived": session.archived}


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
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    message = await svc.add_message(session_id, data, generate_embedding=False)

    # 不在请求链路中同步做摘要或 embedding，优先保证消息落库成功
    return message


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


@router.post(
    "/sessions/{session_id}/search",
    response_model=SearchResponse,
)
async def search_messages(
    session_id: uuid.UUID,
    data: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """基于语义相似度搜索当前会话历史消息"""
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    results = await svc.search_similar_messages(session_id, data.query, data.top_k)
    compressed_results = []
    for msg in results:
        compression = compress_search_result(msg.content, max_length=2048)
        msg_dict = MessageResponse.model_validate(msg).model_dump()
        msg_dict["content"] = compression["content"]
        if compression["truncated"]:
            msg_dict["metadata_json"] = msg_dict.get("metadata_json") or {}
            msg_dict["metadata_json"]["search_truncated"] = {
                "original_size": compression["original_size"],
                "compressed_size": compression["compressed_size"]
            }
        compressed_results.append(MessageResponse(**msg_dict))

    return SearchResponse(
        session_id=session_id,
        query=data.query,
        top_k=data.top_k,
        results=compressed_results,
    )


@router.get("/sessions/{session_id}/kg/entities", response_model=list[KGEntityResponse])
async def get_kg_entities(
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    kg = KnowledgeGraphService(db)
    entities = await kg.list_entities(session_id=session_id, limit=limit)
    return [KGEntityResponse.model_validate(e) for e in entities]


@router.get("/sessions/{session_id}/kg/relations", response_model=list[KGRelationResponse])
async def get_kg_relations(
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    kg = KnowledgeGraphService(db)
    relations = await kg.list_relations(session_id=session_id, limit=limit)
    return [KGRelationResponse.model_validate(r) for r in relations]


@router.post("/sessions/{session_id}/kg/search", response_model=KGSearchResponse)
async def search_kg(
    session_id: uuid.UUID,
    data: KGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> KGSearchResponse:
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    kg = KnowledgeGraphService(db)
    entities, relations = await kg.search(data.query, session_id=session_id, top_k=data.top_k)
    return KGSearchResponse(
        session_id=session_id,
        query=data.query,
        top_k=data.top_k,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )


@router.get("/kg/entities", response_model=list[KGEntityResponse])
async def list_all_kg_entities(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    kg = KnowledgeGraphService(db)
    entities = await kg.list_entities(limit=limit)
    return [KGEntityResponse.model_validate(e) for e in entities]


@router.get("/kg/relations", response_model=list[KGRelationResponse])
async def list_all_kg_relations(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    kg = KnowledgeGraphService(db)
    relations = await kg.list_relations(limit=limit)
    return [KGRelationResponse.model_validate(r) for r in relations]


@router.post("/kg/search", response_model=KGSearchResponse)
async def search_all_kg(
    data: KGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> KGSearchResponse:
    kg = KnowledgeGraphService(db)
    entities, relations = await kg.search(data.query, top_k=data.top_k)
    return KGSearchResponse(
        query=data.query,
        top_k=data.top_k,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )


@router.get("/kg/entities/by-visibility", response_model=list[KGEntityResponse])
async def list_kg_entities_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGEntityResponse]:
    stmt = select(KGEntity).join(Session, KGEntity.session_id == Session.id).where(Session.visibility == visibility).order_by(KGEntity.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return [KGEntityResponse.model_validate(e) for e in result.scalars().all()]


@router.get("/kg/relations/by-visibility", response_model=list[KGRelationResponse])
async def list_kg_relations_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[KGRelationResponse]:
    stmt = select(KGRelation).join(Session, KGRelation.session_id == Session.id).where(Session.visibility == visibility).order_by(KGRelation.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return [KGRelationResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/kg/graph/by-visibility", response_model=KGGraphResponse)
async def get_kg_graph_by_visibility(
    visibility: str = Query(..., pattern=r"^(team|public)$"),
    limit: int = Query(80, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    entity_stmt = select(KGEntity).join(Session, KGEntity.session_id == Session.id).where(Session.visibility == visibility).order_by(KGEntity.created_at.desc()).limit(limit)
    relation_stmt = select(KGRelation).join(Session, KGRelation.session_id == Session.id).where(Session.visibility == visibility).order_by(KGRelation.created_at.desc()).limit(limit * 2)
    entities = list((await db.execute(entity_stmt)).scalars().all())
    relations = list((await db.execute(relation_stmt)).scalars().all())
    nodes = [{"id": str(e.id), "label": e.name, "type": e.entity_type, "session_id": e.session_id} for e in entities]
    edges = [{"id": str(r.id), "source": str(r.source_entity_id), "target": str(r.target_entity_id), "relation_type": r.relation_type, "session_id": r.session_id} for r in relations]
    return KGGraphResponse(session_id=None, node_count=len(nodes), edge_count=len(edges), nodes=nodes, edges=edges)


@router.post("/query", response_model=UnifiedQueryResponse)
async def unified_query(
    data: UnifiedQueryRequest,
    db: AsyncSession = Depends(get_db),
) -> UnifiedQueryResponse:
    session_id = data.session_id
    messages = []
    entities = []
    relations = []

    if data.mode in ("vector", "hybrid") and session_id is not None:
        svc = SessionService(db)
        vector_results = await svc.search_similar_messages(session_id, data.query, data.top_k)
        messages = [MessageResponse.model_validate(m) for m in vector_results]

    if data.mode in ("graph", "hybrid"):
        kg = KnowledgeGraphService(db)
        kg_entities, kg_relations = await kg.search(data.query, session_id=session_id, top_k=data.top_k)
        entities = [KGEntityResponse.model_validate(e) for e in kg_entities]
        relations = [KGRelationResponse.model_validate(r) for r in kg_relations]

    return UnifiedQueryResponse(
        mode=data.mode,
        query=data.query,
        session_id=session_id,
        top_k=data.top_k,
        messages=messages,
        entities=entities,
        relations=relations,
    )


@router.get("/kg/graph", response_model=KGGraphResponse)
async def get_kg_graph(
    session_id: uuid.UUID | None = Query(None),
    limit: int = Query(80, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
) -> KGGraphResponse:
    kg = KnowledgeGraphService(db)
    nodes, edges = await kg.get_graph(session_id=session_id, limit=limit)
    return KGGraphResponse(
        session_id=session_id,
        node_count=len(nodes),
        edge_count=len(edges),
        nodes=nodes,
        edges=edges,
    )


@router.post("/plugin-state/session-map", response_model=PluginStateResponse)
async def set_plugin_session_map_state(
    data: PluginSessionMapState,
) -> PluginStateResponse:
    key = f"session-map:{data.opencode_session_id}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/session-map/{opencode_session_id}", response_model=PluginStateResponse)
async def get_plugin_session_map_state(
    opencode_session_id: str,
) -> PluginStateResponse:
    key = f"session-map:{opencode_session_id}"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.post("/plugin-state/message-state", response_model=PluginStateResponse)
async def set_plugin_message_state(
    data: PluginMessageState,
) -> PluginStateResponse:
    key = f"message-state:{data.session_id}:{data.message_id}"
    value = data.model_dump(mode="json")
    await cache.set_plugin_state(key, value)
    return PluginStateResponse(key=key, value=value)


@router.get("/plugin-state/message-state/{session_id}/{message_id}", response_model=PluginStateResponse)
async def get_plugin_message_state(
    session_id: str,
    message_id: str,
) -> PluginStateResponse:
    key = f"message-state:{session_id}:{message_id}"
    value = await cache.get_plugin_state(key)
    return PluginStateResponse(key=key, value=value)


@router.post("/plugin-state/continuation", response_model=PluginStateResponse)
async def set_plugin_continuation_state(
    data: PluginContinuationState,
) -> PluginStateResponse:
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
async def get_plugin_continuation_state(
    source_session_id: str,
) -> PluginStateResponse:
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
        "consumed_at": value.get("consumed_at") or __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    await cache.set_plugin_state(key, value)
    latest_value = await cache.get_plugin_state("continuation:latest")
    if latest_value and latest_value.get("source_session_id") == source_session_id:
        await cache.set_plugin_state("continuation:latest", value)
    return PluginStateResponse(key=key, value=value)


@router.post("/admin/runtime-events/opencode")
async def ingest_opencode_runtime_event(
    data: OpenCodeRuntimeEventPayload,
) -> PluginStateResponse:
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
    session = await svc.get_session(session_id)
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

    # 5. 在主链路成功后按配置尝试触发摘要，不影响聊天主流程
    try:
        if settings.enable_auto_summary:
            await check_and_trigger_summary(db, session_id)
    except Exception:
        pass

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
    """获取会话的上下文使用统计"""
    from src.config import settings

    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    summary_svc = SummaryService(db)
    summary = await summary_svc.get_latest_summary(session_id)
    summaries_count = len(session.summaries) if session.summaries else 0

    # 估算当前上下文窗口大小
    builder = ContextBuilder(db)
    ctx = await builder.build(session_id, "test query")
    ctx_tokens = ctx.total_tokens

    graph_hits = 0
    graph_context_tokens = 0
    if ctx.graph_context:
        graph_hits = len([line for line in ctx.graph_context.split("\n") if line.strip()])
        graph_context_tokens = count_tokens(ctx.graph_context)

    return ContextStats(
        session_id=session_id,
        total_messages=session.message_count,
        total_tokens=session.total_tokens,
        summaries_count=summaries_count,
        context_window_tokens=ctx_tokens,
        context_utilization=min(ctx_tokens / settings.max_context_tokens, 1.0),
        budget_state=ctx.budget_state,
        budget_ratio=ctx.budget_ratio,
        system_prompt_tokens=ctx.system_prompt_tokens,
        user_message_tokens=ctx.user_message_tokens,
        summary_enabled=settings.enable_auto_summary,
        summary_available=bool(summary and getattr(summary, "content", None)),
        summary_tokens=ctx.summary_tokens,
        shared_summary_tokens=ctx.shared_summary_tokens,
        graph_hits=graph_hits,
        graph_context_tokens=graph_context_tokens,
        graph_context_utilization=min(graph_context_tokens / settings.max_context_tokens, 1.0),
        shared_graph_tokens=ctx.shared_graph_tokens,
        retrieved_tokens=ctx.retrieved_tokens,
        recent_tokens=ctx.recent_tokens,
        reply_reserve_tokens=ctx.reply_reserve_tokens,
    )


# ── 手动触发摘要 ──────────────────────────────────


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


@router.post("/sessions/{session_id}/summarize", status_code=200)
async def trigger_summary(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动触发摘要生成"""
    svc = SessionService(db)
    session = await svc.get_session(session_id)
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


@router.get("/sessions/{session_id}/summary-status", response_model=SummaryStatusResponse)
async def get_summary_status(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SummaryStatusResponse:
    svc = SessionService(db)
    session = await svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    summary_svc = SummaryService(db)
    return SummaryStatusResponse(**(await summary_svc.get_summary_status(session_id)))


@router.get("/projects/{project_id}/search/summaries", response_model=list[str])
async def search_project_summaries(
    project_id: str,
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    from src.models import Summary, Session
    stmt = (
        select(Summary.content)
        .join(Session, Summary.session_id == Session.id)
        .where(Session.metadata_json['project_id'].astext == project_id)
        .order_by(Summary.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


@router.get("/projects/{project_id}/search/kg", response_model=ProjectSharedContextResponse)
async def search_project_kg(
    project_id: str,
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> ProjectSharedContextResponse:
    from src.models import Summary, KGEntity, KGRelation, Session
    entity_stmt = (
        select(KGEntity)
        .join(Session, KGEntity.session_id == Session.id)
        .where(Session.metadata_json['project_id'].astext == project_id)
        .order_by(KGEntity.created_at.desc())
        .limit(limit)
    )
    relation_stmt = (
        select(KGRelation)
        .join(Session, KGRelation.session_id == Session.id)
        .where(Session.metadata_json['project_id'].astext == project_id)
        .order_by(KGRelation.created_at.desc())
        .limit(limit)
    )
    summary_stmt = (
        select(Summary.content)
        .join(Session, Summary.session_id == Session.id)
        .where(Session.metadata_json['project_id'].astext == project_id)
        .order_by(Summary.created_at.desc())
        .limit(limit)
    )
    entities = list((await db.execute(entity_stmt)).scalars().all())
    relations = list((await db.execute(relation_stmt)).scalars().all())
    summaries = [row[0] for row in (await db.execute(summary_stmt)).all()]
    return ProjectSharedContextResponse(
        project_id=project_id,
        summaries=summaries,
        entities=[KGEntityResponse.model_validate(e) for e in entities],
        relations=[KGRelationResponse.model_validate(r) for r in relations],
    )

# ── Dashboard 聚合（只读，读取 logs/audits/*_latest.json）──────────────────────

@router.post("/admin/ops/repair-stuck-session")
async def repair_stuck_session(session_id: str = Query(..., min_length=1), apply: bool = Query(False), db: AsyncSession = Depends(get_db)) -> dict:
    payload = {
        "status": "desktop_required",
        "session_id": session_id,
        "apply": apply,
        "message": "OpenCode sqlite 数据库位于桌面端 10.100.1.18，本接口所在 API 容器不直接持有该数据库。请在 10.100.1.18 本机执行 repair_stuck_opencode_sessions.py。",
        "verified_command": f"python3 /home/jimwong/opencode/repair_stuck_opencode_sessions.py --session {session_id}" + (" --apply" if apply else ""),
    }
    await _record_operation_history(db, action="repair_stuck_session", target_type="opencode_session", target_id=session_id, status="desktop_required", message=payload["verified_command"])
    return payload


@router.post("/admin/ops/reconcile-continuation-metrics")
async def reconcile_continuation_metrics(db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import datetime, timezone
    from src.metrics import CONTINUATION_CONSUMED_TOTAL, CONTINUATION_LAST_AGE_SECONDS, CONTINUATION_PENDING_GAUGE, CONTINUATION_TRIGGER_TOTAL
    latest = await cache.get_plugin_state("continuation:latest")
    if not latest:
        CONTINUATION_PENDING_GAUGE.observe(0)
        return {"status": "ok", "message": "no latest continuation"}

    failure_code = latest.get("failure_code") or "unknown"
    created_at = latest.get("created_at")
    consumed = 1 if latest.get("consumed_by_session_id") else 0
    pending = 0 if consumed else 1
    age = None
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            age = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        except Exception:
            age = None

    CONTINUATION_TRIGGER_TOTAL.labels(failure_code=failure_code).inc()
    if consumed:
        CONTINUATION_CONSUMED_TOTAL.inc()
    CONTINUATION_PENDING_GAUGE.observe(float(pending))
    if age is not None:
        CONTINUATION_LAST_AGE_SECONDS.observe(float(age))

    await _record_operation_history(db, action="reconcile_continuation_metrics", target_type="continuation", target_id=failure_code or "none", status="ok", message=f"pending={pending},consumed={consumed},age={age}")
    return {
        "status": "ok",
        "failure_code": failure_code,
        "pending": pending,
        "consumed": consumed,
        "latest_age_seconds": age,
    }


@router.post("/admin/ops/requeue-kg-deadletters")
async def requeue_kg_deadletters(reason: str | None = Query(None), db: AsyncSession = Depends(get_db)) -> dict:
    from sqlalchemy.sql import text

    if reason:
        result = await db.execute(
            text("update kg_jobs set status='pending', attempts=0, last_error=null, available_at=now(), locked_at=null, locked_by=null, updated_at=now() where status='dead_letter' and last_error = :reason returning id"),
            {"reason": reason},
        )
    else:
        result = await db.execute(
            text("update kg_jobs set status='pending', attempts=0, last_error=null, available_at=now(), locked_at=null, locked_by=null, updated_at=now() where status='dead_letter' returning id")
        )
    rows = list(result.all())
    await db.commit()
    await _record_operation_history(db, action="requeue_kg_deadletters", target_type="kg_jobs", target_id=reason or "all", status="ok", message=f"requeued={len(rows)}")
    return {"status": "ok", "requeued": len(rows), "reason": reason}


@router.post("/admin/ops/run-kg-worker-once")
async def run_kg_worker_once(batch_size: int = Query(20, ge=1, le=100), sleep_seconds: float = Query(0.01, ge=0.0, le=1.0), db: AsyncSession = Depends(get_db)) -> dict:
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        "/opt/conda/bin/python",
        "/app/scripts/backfill_kg_jobs.py",
        "--apply",
        "--batch-size", str(batch_size),
        "--sleep-seconds", str(sleep_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await proc.communicate()
    text_out = output.decode("utf-8", errors="ignore")
    status = "ok" if proc.returncode == 0 else "error"
    await _record_operation_history(db, action="run_kg_worker_once", target_type="kg_jobs", target_id=f"batch={batch_size}", status=status, message=text_out[-500:])
    return {"status": status, "exit_code": proc.returncode, "output": text_out[-4000:]}


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


@router.get("/admin/dashboard", response_model=AdminDashboardResponse)
async def get_admin_dashboard() -> AdminDashboardResponse:
    """
    聚合所有巡检结果的只读 Dashboard 接口。
    返回 health / continuation / duplicates / kg / capacity / alerts / trends / shared_memory 八个子区块，
    数据来源为 logs/audits/*_latest.json，无需数据库查询。
    """
    from src.services.audit_report_service import get_admin_dashboard
    return await get_admin_dashboard()
