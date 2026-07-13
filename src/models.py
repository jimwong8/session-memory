"""SQLAlchemy 数据模型 - 会话、消息、摘要、知识图谱"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config import settings


class Base(DeclarativeBase):
    """ORM 基类"""

    pass


class Session(Base):
    """会话模型 - 存储会话元信息"""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    archived: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    canvas_mermaid: Mapped[str | None] = mapped_column(Text, nullable=True)
    pyramid_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.created_at"
    )
    summaries: Mapped[list["Summary"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Summary.created_at.desc()"
    )

    __table_args__ = (
        Index("idx_sessions_user_updated", "user_id", "updated_at"),
    )


class Message(Base):
    """消息模型 - 存储单条对话消息及其向量"""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(384), nullable=True
    )

    session: Mapped["Session"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("idx_messages_session_created", "session_id", "created_at"),
        Index(
            "uq_messages_auto_message_version",
            "session_id",
            text("(metadata_json->>'opencode_message_id')"),
            text("(metadata_json->>'content_hash')"),
            unique=True,
            postgresql_where=text(
                "metadata_json->>'source' = 'session-memory-auto' "
                "AND metadata_json->>'event' = 'message.updated' "
                "AND metadata_json ? 'opencode_message_id' "
                "AND metadata_json ? 'content_hash' "
                "AND role IN ('user', 'assistant')"
            ),
        ),
        Index(
            "idx_messages_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Summary(Base):
    """摘要模型 - 存储会话的压缩摘要"""

    __tablename__ = "summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    message_range_end: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped["Session"] = relationship(back_populates="summaries")

    __table_args__ = (
        Index("idx_summaries_session_created", "session_id", "created_at"),
    )


class KGEntity(Base):
    """知识图谱实体"""

    __tablename__ = "kg_entities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    level: Mapped[str] = mapped_column(String(8), default="L1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_kg_entities_session_name", "session_id", "name"),
    )


class KGRelation(Base):
    """知识图谱关系"""

    __tablename__ = "kg_relations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_kg_relations_session_type", "session_id", "relation_type"),
    )

class OperationHistory(Base):
    __tablename__ = "operation_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class KGJob(Base):
    """知识图谱提取任务"""

    __tablename__ = "kg_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False, default="extract")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("uq_kg_jobs_message_type", "message_id", "job_type", unique=True),
        Index("idx_kg_jobs_status_available", "status", "available_at", "created_at"),
        Index("idx_kg_jobs_session_created", "session_id", "created_at"),
    )


class MemoryAtom(Base):
    """L1 记忆原子 - 原子事实"""

    __tablename__ = "memory_atoms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    project_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(32), default="fact",
        nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True, default=list)
    source_message_ids: Mapped[list | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, default=0.8)
    sensitivity_level: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    dedup_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_atoms.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_atoms_user_kind", "user_id", "kind", "created_at"),
        Index("idx_atoms_dedup", "user_id", "dedup_signature",
              postgresql_where=text("dedup_signature IS NOT NULL")),
    )


class MemoryScenario(Base):
    """L2 场景块 - 聚合原子事实的场景叙事"""

    __tablename__ = "memory_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    project_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    narrative_md: Mapped[str] = mapped_column(Text, nullable=False)
    atom_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True, default=list)
    session_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True, default=list)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_scenarios_user", "user_id", "created_at"),
    )


class Persona(Base):
    """L3 用户画像 - 个人偏好与长期特征"""

    __tablename__ = "personas"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    profile_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    preferences_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    scenario_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PersonaHistory(Base):
    """L3 用户画像历史版本"""

    __tablename__ = "persona_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_md: Mapped[str] = mapped_column(Text, nullable=False)
    preferences_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_persona_history_user", "user_id", "version"),
    )
