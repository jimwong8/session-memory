"""上下文构建器 - 智能构建 LLM 上下文窗口"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.metrics import CONTEXT_BUDGET_RATIO, CONTEXT_BUDGET_STATE, CONTEXT_WINDOW_TOKENS
from src.models import KGEntity, Session, Summary
from src.schemas import ContextWindow, MessageResponse
from src.services.knowledge_graph import KnowledgeGraphService
from src.services.session_service import SessionService
from src.services.summary_service import SummaryService
from src.tokenizer import count_tokens

logger = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.session_svc = SessionService(db)
        self.summary_svc = SummaryService(db)
        self.kg_svc = KnowledgeGraphService(db)

    async def build(
        self,
        session_id: uuid.UUID,
        user_message: str,
        system_prompt: str | None = None,
    ) -> ContextWindow:
        budget = settings.max_context_tokens
        ctx = ContextWindow()

        if system_prompt:
            ctx.system_prompt = system_prompt
            ctx.system_prompt_tokens = count_tokens(system_prompt) + 4
            budget -= ctx.system_prompt_tokens

        ctx.user_message_tokens = count_tokens(user_message) + 4
        budget -= ctx.user_message_tokens

        ctx.reply_reserve_tokens = settings.max_context_tokens // 4
        budget -= ctx.reply_reserve_tokens

        if budget <= 0:
            logger.warning("上下文预算不足，仅发送用户消息")
            ctx.total_tokens = ctx.user_message_tokens + ctx.system_prompt_tokens
            ctx.budget_ratio = min(ctx.total_tokens / settings.max_context_tokens, 1.0)
            ctx.budget_state = 'force_continuation'
            CONTEXT_WINDOW_TOKENS.observe(ctx.total_tokens)
            CONTEXT_BUDGET_RATIO.observe(ctx.budget_ratio)
            CONTEXT_BUDGET_STATE.labels(state=ctx.budget_state).inc()
            return ctx

        session_stmt = select(Session).where(Session.id == session_id)
        session_obj = (await self.db.execute(session_stmt)).scalar_one_or_none()
        project_id = None
        if session_obj and session_obj.metadata_json:
            project_id = session_obj.metadata_json.get('project_id')

        summary = await self.summary_svc.get_latest_summary(session_id)
        if summary and summary.content:
            summary_tokens = count_tokens(summary.content) + 4
            if summary_tokens < budget * 0.3:
                ctx.summary = summary.content
                ctx.summary_tokens = summary_tokens
                budget -= summary_tokens

        if project_id:
            try:
                shared_summary_stmt = (
                    select(Summary.content)
                    .join(Session, Summary.session_id == Session.id)
                    .where(Session.id != session_id)
                    .where(Session.metadata_json['project_id'].astext == project_id)
                    .order_by(Summary.created_at.desc())
                    .limit(1)
                )
                shared_summary = (await self.db.execute(shared_summary_stmt)).scalar_one_or_none()
                if shared_summary:
                    shared_tokens = count_tokens(shared_summary) + 4
                    if shared_tokens < budget * 0.2:
                        ctx.shared_summary = shared_summary
                        ctx.shared_summary_tokens = shared_tokens
                        budget -= shared_tokens
            except Exception:
                logger.warning("项目级摘要共享查询失败，跳过", exc_info=True)

        try:
            kg_entities, kg_relations = await self.kg_svc.search(user_message, session_id=session_id, top_k=5)
            if kg_entities or kg_relations:
                entity_lines = [f"[entity:{e.entity_type}] {e.name}" for e in kg_entities]
                relation_lines = [f"[relation] {r.relation_type}" for r in kg_relations]
                graph_context = "\n".join(entity_lines + relation_lines)
                graph_tokens = count_tokens(graph_context) + 4
                if graph_tokens < budget * settings.max_graph_context_ratio:
                    ctx.graph_context = graph_context
                    ctx.graph_tokens = graph_tokens
                    budget -= graph_tokens
        except Exception:
            logger.warning("知识图谱查询失败，跳过", exc_info=True)

        if project_id:
            try:
                shared_entity_stmt = (
                    select(KGEntity)
                    .join(Session, KGEntity.session_id == Session.id)
                    .where(Session.id != session_id)
                    .where(Session.metadata_json['project_id'].astext == project_id)
                    .order_by(KGEntity.created_at.desc())
                    .limit(5)
                )
                shared_entities = list((await self.db.execute(shared_entity_stmt)).scalars().all())
                if shared_entities:
                    shared_graph = "\n".join(f"[shared-entity:{e.entity_type}] {e.name}" for e in shared_entities)
                    shared_graph_tokens = count_tokens(shared_graph) + 4
                    if shared_graph_tokens < budget * settings.max_graph_context_ratio:
                        ctx.shared_graph_context = shared_graph
                        ctx.shared_graph_tokens = shared_graph_tokens
                        budget -= shared_graph_tokens
            except Exception:
                logger.warning("项目级知识图谱共享查询失败，跳过", exc_info=True)

        try:
            retrieved = await self.session_svc.search_similar_messages(session_id, user_message)
            for msg in retrieved:
                msg_tokens = count_tokens(msg.content) + 4
                if msg_tokens > budget * 0.2:
                    continue
                if budget - msg_tokens < 0:
                    break
                ctx.retrieved_messages.append(MessageResponse.model_validate(msg))
                ctx.retrieved_tokens += msg_tokens
                budget -= msg_tokens
        except Exception:
            logger.warning("向量检索失败，跳过", exc_info=True)

        recent = await self.session_svc.get_recent_messages(session_id)
        added_recent: list[MessageResponse] = []
        for msg in reversed(recent):
            msg_tokens = count_tokens(msg.content) + 4
            if budget - msg_tokens < 0:
                break
            added_recent.insert(0, MessageResponse.model_validate(msg))
            ctx.recent_tokens += msg_tokens
            budget -= msg_tokens
        ctx.recent_messages = added_recent

        ctx.total_tokens = settings.max_context_tokens - budget - ctx.reply_reserve_tokens
        ctx.budget_ratio = min(ctx.total_tokens / settings.max_context_tokens, 1.0)
        if ctx.budget_ratio >= settings.context_force_continuation_ratio:
            ctx.budget_state = 'force_continuation'
        elif ctx.budget_ratio >= settings.context_prepare_continuation_ratio:
            ctx.budget_state = 'prepare_continuation'
        elif ctx.budget_ratio >= settings.context_warn_ratio:
            ctx.budget_state = 'warning'
        else:
            ctx.budget_state = 'normal'

        CONTEXT_WINDOW_TOKENS.observe(ctx.total_tokens)
        CONTEXT_BUDGET_RATIO.observe(ctx.budget_ratio)
        CONTEXT_BUDGET_STATE.labels(state=ctx.budget_state).inc()
        return ctx

    def to_messages(
        self,
        ctx: ContextWindow,
        user_message: str,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []

        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})

        if ctx.summary:
            messages.append({
                "role": "system",
                "content": f"[SESSION_SUMMARY]\n以下是当前会话的摘要，优先级最高，代表当前任务上下文。\n{ctx.summary}",
            })

        if ctx.shared_summary:
            messages.append({
                "role": "system",
                "content": f"[PROJECT_SUMMARY]\n以下内容来自同一 project_id 的其他会话，仅用于提供背景参考。若与当前会话冲突，以当前会话中的最新事实为准。\n{ctx.shared_summary}",
            })

        if ctx.graph_context:
            messages.append({
                "role": "system",
                "content": f"[SESSION_GRAPH]\n以下是当前会话中与本次问题相关的知识图谱命中，用于补充当前会话事实。\n{ctx.graph_context}",
            })

        if ctx.shared_graph_context:
            messages.append({
                "role": "system",
                "content": f"[PROJECT_GRAPH]\n以下内容来自同一 project_id 的其他会话知识图谱，仅作弱参考；如果与当前会话中的事实冲突，以当前会话为准。\n{ctx.shared_graph_context}",
            })

        if ctx.retrieved_messages:
            retrieved_text = "\n".join(
                f"[{m.role}]: {m.content}" for m in ctx.retrieved_messages
            )
            messages.append({
                "role": "system",
                "content": f"[SESSION_RETRIEVED]\n以下是当前会话中与本次问题语义相近的历史消息，仅在有助于解决当前问题时参考。\n{retrieved_text}",
            })

        for msg in ctx.recent_messages:
            content = msg.content or ""
            metadata = msg.metadata_json or {}
            event_name = metadata.get("event") if isinstance(metadata, dict) else None
            if msg.role == "system" and (
                event_name in {"session.error", "session.continuation.injected", "tool.execute.before", "tool.execute.after"}
                or "[SYSTEM DIRECTIVE: OH-MY-OPENCODE - TODO CONTINUATION]" in content
                or "<!-- OMO_INTERNAL_INITIATOR -->" in content
                or "[自动续接上下文]" in content
            ):
                continue
            messages.append({"role": msg.role, "content": msg.content})

        messages.append({"role": "user", "content": user_message})
        return messages


async def check_and_trigger_summary(
    db: AsyncSession, session_id: uuid.UUID
) -> None:
    svc = SummaryService(db)
    if await svc.should_generate_summary(session_id):
        logger.info("触发摘要生成: %s", session_id)
        await svc.generate_and_save_summary(session_id)
