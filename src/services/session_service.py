"""会话管理核心服务"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.cache import cache
from src.embedding_service import create_embedding
from src.metrics import (
    AUTO_MESSAGE_DEDUPE_CONFLICTS,
    AUTO_MESSAGE_DEDUPE_HITS,
    COMPRESSION_APPLIED,
    COMPRESSION_RATIO,
    EMBEDDING_DURATION,
    EMBEDDING_FAIL_COUNT,
    EMBEDDING_SUCCESS_COUNT,
    MESSAGE_SIZE_BYTES,
    MESSAGES_TOTAL,
    SESSIONS_TOTAL,
)
from src.models import KGJob, Message, Session
from src.schemas import MessageCreate, MessageResponse, SessionCreate, SessionResponse
from src.tokenizer import count_tokens
from src.utils.compress_message import compress_message_content
from src.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)


class SessionService:
    """会话 CRUD 和消息管理"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _extract_auto_message_identity(self, data: MessageCreate) -> dict | None:
        metadata = data.metadata_json or {}
        if data.role not in ("user", "assistant"):
            return None
        if metadata.get("source") != "session-memory-auto":
            return None
        if metadata.get("event") != "message.updated":
            return None

        opencode_message_id = metadata.get("opencode_message_id")
        content_hash = metadata.get("content_hash")
        if not opencode_message_id or not content_hash:
            return None

        return {
            "opencode_message_id": str(opencode_message_id),
            "content_hash": str(content_hash),
        }

    async def _find_existing_auto_message(
        self,
        session_id: uuid.UUID,
        data: MessageCreate,
    ) -> Message | None:
        identity = self._extract_auto_message_identity(data)
        if not identity:
            return None

        stmt = select(Message).where(
            Message.session_id == session_id,
            Message.role == data.role,
            func.jsonb_extract_path_text(Message.metadata_json, "source") == "session-memory-auto",
            func.jsonb_extract_path_text(Message.metadata_json, "event") == "message.updated",
            func.jsonb_extract_path_text(Message.metadata_json, "opencode_message_id") == identity["opencode_message_id"],
            func.jsonb_extract_path_text(Message.metadata_json, "content_hash") == identity["content_hash"],
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_session(self, data: SessionCreate) -> Session:
        """创建新会话（对 opencode_session_id 做幂等保护）"""
        metadata = data.metadata_json or {}
        opencode_session_id = metadata.get("opencode_session_id")
        if opencode_session_id:
            stmt = (
                select(Session)
                .where(Session.user_id == data.user_id)
                .where(func.jsonb_extract_path_text(Session.metadata_json, "opencode_session_id") == str(opencode_session_id))
                .order_by(Session.updated_at.desc())
                .limit(1)
            )
            existing = (await self.db.execute(stmt)).scalar_one_or_none()
            if existing:
                return existing

        session = Session(
            user_id=data.user_id,
            title=data.title,
            metadata_json=metadata,
        )
        self.db.add(session)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            if opencode_session_id:
                stmt = (
                    select(Session)
                    .where(Session.user_id == data.user_id)
                    .where(func.jsonb_extract_path_text(Session.metadata_json, "opencode_session_id") == str(opencode_session_id))
                    .order_by(Session.updated_at.desc())
                    .limit(1)
                )
                existing = (await self.db.execute(stmt)).scalar_one_or_none()
                if existing:
                    return existing
            raise
        await self.db.refresh(session)

        await cache.set_session_meta(
            session.id,
            SessionResponse.model_validate(session).model_dump(mode="json"),
        )
        SESSIONS_TOTAL.inc()
        return session

    async def get_session(
        self,
        db: AsyncSession,
        session_id: str,
        message_limit: int = 50,
    ) -> tuple[Session, list[Message]]:
        """获取会话基本信息及分页消息（而非全量加载）"""
        from fastapi import HTTPException
        # 加载会话基本信息
        stmt = select(Session).where(Session.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        
        # 分页加载消息（而非全量 selectinload）
        msg_stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(message_limit)
        )
        msg_result = await db.execute(msg_stmt)
        messages = list(msg_result.scalars().all())
        
        return session, messages

    async def list_sessions(self, user_id: str, limit: int = 20, offset: int = 0) -> list[Session]:
        """列出用户的会话"""
        stmt = (
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def delete_session(self, session_id: uuid.UUID) -> bool:
        """删除会话及其所有消息"""
        session = await self.db.get(Session, session_id)
        if not session:
            return False
        await self.db.delete(session)
        await self.db.commit()
        await cache.clear_session(session_id)
        return True

    async def add_message(
        self,
        session_id: uuid.UUID,
        data: MessageCreate,
        generate_embedding: bool = True,
    ) -> Message:
        """添加消息到会话：先落库，再尽力生成 embedding，不阻塞主流程。"""
        existing = await self._find_existing_auto_message(session_id, data)
        if existing:
            AUTO_MESSAGE_DEDUPE_HITS.labels(path="precheck").inc()
            logger.info(
                "检测到自动消息重复写入，直接复用已有记录",
                extra={"session_id": str(session_id), "message_id": str(existing.id)},
            )
            return existing

        # 压缩消息内容
        original_content = data.content
        compression_result = compress_message_content(original_content)
        compressed_content = compression_result["content"]
        compression_stats = compression_result["stats"]
        
        # Phase 1.2: 记录消息长度和压缩效果
        MESSAGE_SIZE_BYTES.observe(compression_stats["original_size"])
        
        if compression_stats["compressed"]:
            COMPRESSION_APPLIED.labels(strategy=compression_stats["strategy"]).inc()
            COMPRESSION_RATIO.observe(compression_stats["compression_ratio"])
            
            logger.info(
                "消息内容已压缩",
                extra={
                    "session_id": str(session_id),
                    "original_size": compression_stats["original_size"],
                    "compressed_size": compression_stats["compressed_size"],
                    "compression_ratio": f"{compression_stats['compression_ratio']:.2%}",
                    "strategy": compression_stats["strategy"],
                },
            )

        tokens = count_tokens(compressed_content)

        # 将压缩统计信息添加到 metadata
        metadata = data.metadata_json or {}
        if compression_stats["compressed"]:
            metadata["compression"] = compression_stats

        should_enqueue_kg = data.role in ("user", "assistant") and len(compressed_content or "") >= 50
        if data.role in ("user", "assistant"):
            metadata["kg_extract_pending"] = bool(should_enqueue_kg)
            metadata["kg_extract_status"] = "pending" if should_enqueue_kg else "skipped_short"
            metadata["kg_extract_attempts"] = 0
            metadata["kg_extract_error"] = None if should_enqueue_kg else "content_too_short"
            metadata["kg_extract_next_attempt_at"] = None
        message = Message(
            session_id=session_id,
            role=data.role,
            content=compressed_content,
            tokens=tokens,
            metadata_json=metadata,
            embedding=None,
        )
        self.db.add(message)

        stmt = select(Session).where(Session.id == session_id)
        result = await self.db.execute(stmt)
        session = result.scalar_one_or_none()
        if session:
            session.total_tokens += tokens
            session.message_count += 1

        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing = await self._find_existing_auto_message(session_id, data)
            if existing:
                AUTO_MESSAGE_DEDUPE_CONFLICTS.labels(path="integrity_error").inc()
                logger.info(
                    "自动消息命中数据库幂等约束，返回已有记录",
                    extra={"session_id": str(session_id), "message_id": str(existing.id)},
                )
                return existing
            raise

        await self.db.refresh(message)

        if should_enqueue_kg:
            stmt = insert(KGJob).values(
                message_id=message.id,
                session_id=session_id,
                job_type="extract",
                status="pending",
                attempts=0,
            ).on_conflict_do_nothing(index_elements=["message_id", "job_type"])
            await self.db.execute(stmt)
            await self.db.commit()
            await self.db.refresh(message)

        msg_resp = MessageResponse.model_validate(message)
        await cache.push_message(session_id, msg_resp)
        MESSAGES_TOTAL.inc()

        if generate_embedding and data.role in ("user", "assistant"):
            import time
            start_time = time.time()
            try:
                embedding = await create_embedding(compressed_content)
                message.embedding = embedding
                await self.db.commit()
                await self.db.refresh(message)

                duration = time.time() - start_time
                EMBEDDING_DURATION.observe(duration)
                EMBEDDING_SUCCESS_COUNT.inc()
            except Exception:
                duration = time.time() - start_time
                EMBEDDING_DURATION.observe(duration)
                EMBEDDING_FAIL_COUNT.inc()
                logger.warning("向量嵌入生成失败，但消息已成功落库", exc_info=True)

        # Phase 3: 知识图谱抽取改为异步回填，主链路只打 pending 标记
        if data.role in ("user", "assistant"):
            logger.info(
                "知识图谱提取已改为异步回填",
                extra={"session_id": str(session_id), "message_id": str(message.id)},
            )

        # 统一返回已序列化的消息响应，避免 ORM 对象在路由层再次触发异步加载
        return msg_resp

    async def get_recent_messages(self, session_id: uuid.UUID, count: int | None = None) -> list[Message]:
        """获取最近 N 条消息（优先从缓存）"""
        from src.config import settings

        n = count or settings.recent_messages_count
        cached = await cache.get_recent_messages(session_id, n)
        if cached:
            return [
                Message(
                    id=m.id,
                    session_id=m.session_id,
                    role=m.role,
                    content=m.content,
                    tokens=m.tokens,
                    created_at=m.created_at,
                    metadata_json=m.metadata_json,
                    embedding=None,
                )
                for m in cached
            ]

        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .where(
                ~(
                    (Message.role == "system")
                    & (
                        func.jsonb_extract_path_text(Message.metadata_json, "event").in_(["session.error", "session.continuation.injected", "tool.execute.before", "tool.execute.after"])
                    )
                )
            )
            .order_by(Message.created_at.desc())
            .limit(n)
        )
        result = await self.db.execute(stmt)
        messages = list(reversed(result.scalars().all()))

        for msg in messages:
            await cache.push_message(session_id, MessageResponse.model_validate(msg))

        return messages

    async def get_message_count(self, session_id: uuid.UUID) -> int:
        """获取会话消息总数"""
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def search_similar_messages(
        self,
        session_id: uuid.UUID,
        query_text: str,
        top_k: int | None = None,
    ) -> list[Message]:
        """基于语义相似度检索历史消息"""
        from src.config import settings

        k = top_k or settings.max_retrieved_messages

        try:
            query_embedding = await create_embedding(query_text)
        except Exception:
            logger.warning("查询向量生成失败", exc_info=True)
            return []

        stmt = (
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.embedding.isnot(None),
            )
            .order_by(Message.embedding.cosine_distance(query_embedding))
            .limit(k)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
