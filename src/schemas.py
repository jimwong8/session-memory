"""Pydantic 数据传输对象（DTO / Schema）"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessageCreate(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant|system)$")
    content: str = Field(..., min_length=1)
    metadata_json: dict | None = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    tokens: int
    created_at: datetime
    metadata_json: dict | None = None


class SummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    content: str
    message_range_start: int
    message_range_end: int
    tokens: int
    created_at: datetime


class SessionCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    title: str | None = None
    visibility: str = Field("private", pattern=r"^(private|team|public)$")
    project_id: str | None = None
    metadata_json: dict | None = None
    source_terminal_id: str | None = None


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    title: str | None
    visibility: str
    archived: bool
    created_at: datetime
    updated_at: datetime
    total_tokens: int
    message_count: int
    metadata_json: dict | None = None


class SessionDetail(SessionResponse):
    messages: list[MessageResponse] = []
    summaries: list[SummaryResponse] = []


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    system_prompt: str | None = None


class ChatResponse(BaseModel):
    """聊天回复，含记忆注入可见性"""
    session_id: str
    reply: str
    tokens_used: int = 0
    context_messages_count: int = 0
    summary_included: bool = False
    memory_injected: bool = False
    retrieved_count: int = 0
class ContextWindow(BaseModel):
    system_prompt: str | None = None
    summary: str | None = None
    shared_summary: str | None = None
    retrieved_messages: list[MessageResponse] = []
    recent_messages: list[MessageResponse] = []
    graph_context: str | None = None
    shared_graph_context: str | None = None
    memory_context: str | None = None
    total_tokens: int = 0
    budget_state: str = "normal"
    budget_ratio: float = 0.0
    system_prompt_tokens: int = 0
    user_message_tokens: int = 0
    summary_tokens: int = 0
    shared_summary_tokens: int = 0
    graph_tokens: int = 0
    shared_graph_tokens: int = 0
    retrieved_tokens: int = 0
    recent_tokens: int = 0
    reply_reserve_tokens: int = 0


class ContextStats(BaseModel):
    session_id: uuid.UUID
    total_messages: int
    total_tokens: int
    summaries_count: int
    context_window_tokens: int
    context_utilization: float
    budget_state: str
    budget_ratio: float
    system_prompt_tokens: int
    user_message_tokens: int
    summary_enabled: bool
    summary_available: bool
    summary_tokens: int
    shared_summary_tokens: int
    graph_hits: int
    graph_context_tokens: int
    graph_context_utilization: float
    shared_graph_tokens: int
    retrieved_tokens: int
    recent_tokens: int
    reply_reserve_tokens: int


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(3, ge=1, le=20)
    limit: int = Field(default=10, ge=1, le=50)
    strategy: str = Field(default="hybrid", pattern=r"^(keyword|vector|hybrid)$")


class SearchResponse(BaseModel):
    session_id: uuid.UUID
    query: str
    top_k: int
    results: list[MessageResponse] = []


class PluginSessionMapState(BaseModel):
    opencode_session_id: str = Field(..., min_length=1)
    session_memory_id: str = Field(..., min_length=1)
    host: str | None = None
    updated_at: str | None = None


class PluginMessageState(BaseModel):
    session_id: str = Field(..., min_length=1)
    message_id: str = Field(..., min_length=1)
    content_hash: str = Field(..., min_length=1)
    role: str = Field(..., pattern=r"^(user|assistant)$")
    updated_at: str | None = None


class PluginContinuationMessage(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant|system)$")
    content: str = Field(..., min_length=1)
    created_at: str | None = None


class PluginContinuationState(BaseModel):
    source_session_id: str = Field(..., min_length=1)
    source_session_memory_id: str = Field(..., min_length=1)
    failure_code: str = Field(..., min_length=1)
    failure_message: str = Field(..., min_length=1)
    error_preview: str | None = None
    last_user_message: str | None = None
    recent_messages: list[PluginContinuationMessage] = []
    captured_message_count: int | None = None
    created_at: str | None = None
    host: str | None = None
    consumed_by_session_id: str | None = None
    consumed_at: str | None = None


class PluginStateResponse(BaseModel):
    key: str
    value: dict | None = None


class SummaryStatusResponse(BaseModel):
    summary_enabled: bool
    latest_summary_exists: bool
    latest_summary_time: str | None = None
    latest_summary_tokens: int
    latest_summary_range_start: int | None = None
    latest_summary_range_end: int | None = None


class KGEntityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    name: str
    entity_type: str
    created_at: datetime


class KGRelationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    relation_type: str
    message_id: uuid.UUID | None
    created_at: datetime


class KGSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=20)
    auto_decompose: bool = False
    recency_bias: str = Field("off", pattern=r"^(on|auto|off)$")


class KGSearchResponse(BaseModel):
    session_id: uuid.UUID | None = None
    query: str
    top_k: int
    entities: list[KGEntityResponse] = []
    relations: list[KGRelationResponse] = []
    decomposed_queries: list[str] = []
    recency_bias_active: bool = False


class KGSessionRefResponse(BaseModel):
    session_id: uuid.UUID
    entity_count: int | None = None
    relation_count: int | None = None


class UnifiedQueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    mode: str = Field("hybrid", pattern=r"^(vector|graph|hybrid)$")
    session_id: uuid.UUID | None = None
    top_k: int = Field(5, ge=1, le=20)
    auto_decompose: bool = False
    recency_bias: str = Field("off", pattern=r"^(on|auto|off)$")


class UnifiedQueryResponse(BaseModel):
    mode: str
    query: str
    session_id: uuid.UUID | None = None
    top_k: int
    messages: list[MessageResponse] = []
    entities: list[KGEntityResponse] = []
    relations: list[KGRelationResponse] = []
    decomposed_queries: list[str] = []
    recency_bias_active: bool = False


class KGGraphNode(BaseModel):
    id: str
    label: str
    type: str
    session_id: uuid.UUID


class KGGraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation_type: str
    session_id: uuid.UUID


class KGGraphResponse(BaseModel):
    session_id: uuid.UUID | None = None
    node_count: int
    edge_count: int
    nodes: list[KGGraphNode] = []
    edges: list[KGGraphEdge] = []


class ProjectSharedContextResponse(BaseModel):
    project_id: str
    summaries: list[str] = []
    entities: list[KGEntityResponse] = []
    relations: list[KGRelationResponse] = []
    decomposed_queries: list[str] = []
    recency_bias_active: bool = False

# ── Dashboard 聚合接口 ───────────────────────────────────────────────────────

class AuditSectionBase(BaseModel):
    status: str
    issues: list[str] = []
    generated_at: str | None = None

class AuditHealthSummary(AuditSectionBase):
    service_health: dict | None = None
    db_counts: dict | None = None
    continuation_latest: dict | None = None

class AuditContinuationSummary(AuditSectionBase):
    source_session_id: str | None = None
    failure_code: str | None = None
    consumed_by_session_id: str | None = None

class AuditDuplicatesSummary(AuditSectionBase):
    duplicate_group_count: int = 0

class AuditKGSummary(AuditSectionBase):
    kg_fail_json_extract: int = 0
    kg_fail_429: int = 0
    kg_success: int = 0
    # Real kg_jobs queue breakdown (undeclared fields were silently dropped by
    # pydantic, which is why the dashboard showed None / stale zeros).
    kg_pending: int = 0
    kg_failed: int = 0
    kg_deadletter: int = 0
    kg_running: int = 0
    source: str | None = None

class AuditCapacitySummary(AuditSectionBase):
    total_messages: int = 0
    active_sessions: int = 0
    kg_jobs_pending: int = 0
    kg_jobs_deadletter: int = 0
    kg_jobs_oldest_pending_age_seconds: int = 0
    alert_severity: str = "ok"
    # Embedding coverage + completed/failed counts, so the capacity panel can
    # show real progress instead of a hardcoded 0.
    embed_pct: float = 0.0
    m3_pct: float = 0.0
    embed_done: int = 0
    m3_done: int = 0
    kg_jobs_completed: int = 0
    kg_jobs_failed: int = 0

class AuditAlertsSummary(AuditSectionBase):
    checks_evaluated: int = 0
    issues: list[dict] = []

class AuditTrendsEntry(BaseModel):
    file: str
    status: str
    issues: list[str] = []

class AuditTrendsSummary(BaseModel):
    window_size: int = 0
    trends: dict[str, list[AuditTrendsEntry]] = {}

class SharedMemoryProjectSample(BaseModel):
    project_id: str
    session_count: int = 0
    summary_count: int = 0
    kg_entity_count: int = 0


class AuditSharedMemorySummary(AuditSectionBase):
    project_session_count: int = 0
    distinct_project_count: int = 0
    summary_sources: int = 0
    kg_entity_sources: int = 0
    project_samples: list[SharedMemoryProjectSample] = []

class KGOpsWorkerStat(BaseModel):
    worker_id: str
    count: int


class KGOpsErrorStat(BaseModel):
    reason: str
    count: int


class KGOpsSummary(BaseModel):
    deadletter_reasons: list[KGOpsErrorStat] = []
    pending_reasons: list[KGOpsErrorStat] = []
    running_workers: list[KGOpsWorkerStat] = []
    pending_total: int = 0
    queue_ready_total: int = 0
    queue_retry_wait_total: int = 0
    queue_cooldown_running_total: int = 0
    running_total: int = 0
    succeeded_total: int = 0
    deadletter_total: int = 0


class ContinuationMetricsSummary(BaseModel):
    pending: int = 0
    has_latest: bool = False
    latest_age_seconds: int | None = None
    failure_code: str | None = None
    consumed: int = 0


class ContinuationOpsSummary(BaseModel):
    auto_continued_count: int = 0
    last_consumed_exists: bool = False
    last_consumed_target_session_id: str | None = None
    latest_source_session_id: str | None = None
    latest_consumed_by_session_id: str | None = None


class LLMRouteMetricsSummary(BaseModel):
    hit_counts: dict[str, int] = {}
    fail_counts: dict[str, int] = {}
    fallback_counts: dict[str, int] = {}
    circuit_open_counts: dict[str, int] = {}
    window_hit_counts: dict[str, int] = {}
    window_fail_counts: dict[str, int] = {}
    window_fallback_counts: dict[str, int] = {}
    window_circuit_open_counts: dict[str, int] = {}


class OperationHistoryItem(BaseModel):
    action: str
    target_type: str | None = None
    target_id: str | None = None
    status: str
    message: str | None = None
    operator: str | None = None
    created_at: str | None = None


class AIOpsAdviceAction(BaseModel):
    priority: str = "medium"
    area: str = "general"
    action: str
    risk: str | None = None


class AIOpsAdviceSummary(AuditSectionBase):
    summary: str = ""
    risk_level: str = "unknown"
    recommended_actions: list[AIOpsAdviceAction] = []
    model_used: str | None = None
    error: str | None = None


class OpenCodeFlaggedSession(BaseModel):
    session_id: str | None = None
    message_id: str | None = None
    stale_age_seconds: int | None = None


class OpenCodeRepeatOffender(BaseModel):
    session_id: str
    count: int = 0
    last_seen: str | None = None
    recent_message_ids: list[str] = []


class OpenCodeRepeatOffenderAction(BaseModel):
    session_id: str
    count: int = 0
    suggested_action: str
    reason: str
    recent_message_ids: list[str] = []


class OpenCodeRuntimeEventPayload(BaseModel):
    source_host: str
    generated_at: str | None = None
    status: str = "unknown"
    issues: list[str] = []
    dangling_assistant_count: int = 0
    flagged_sessions: list[OpenCodeFlaggedSession] = []
    recent_desktop_errors: int = 0
    recent_cli_errors: int = 0
    recent_plugin_errors: int = 0
    error_classes: dict[str, int] = {}
    raw_audit: dict | None = None


class OpenCodeRuntimeAuditSummary(AuditSectionBase):
    flagged_sessions: list[OpenCodeFlaggedSession] = []
    repeat_offender_sessions: list[OpenCodeRepeatOffender] = []
    repeat_offender_actions: list[OpenCodeRepeatOffenderAction] = []
    sqlite: dict | None = None
    plugin_state: dict | None = None
    logs: dict | None = None
    error_classes: dict[str, int] = {}


class AdminDashboardResponse(BaseModel):
    generated_at: str
    overall_status: str  # "ok" | "warning" | "critical"
    health: AuditHealthSummary
    continuation: AuditContinuationSummary
    duplicates: AuditDuplicatesSummary
    kg: AuditKGSummary
    capacity_snapshot: AuditCapacitySummary
    alerts: AuditAlertsSummary
    trends: AuditTrendsSummary
    shared_memory: AuditSharedMemorySummary
    kg_ops: KGOpsSummary
    continuation_metrics: ContinuationMetricsSummary
    continuation_ops: ContinuationOpsSummary
    llm_route_metrics: LLMRouteMetricsSummary
    operation_history: list[OperationHistoryItem] = []
    ai_ops_advice: AIOpsAdviceSummary
    opencode_runtime: OpenCodeRuntimeAuditSummary
    bridge: dict = {}
    raw_events: dict = {}

class GlobalModelConfig(BaseModel):
    openai_model: str
    summary_model: str
    backup_openai_model: str = ""
    backup_summary_model: str = ""
    tertiary_openai_model: str = ""
    tertiary_summary_model: str = ""
    quaternary_openai_model: str = ""
    quaternary_summary_model: str = ""
    llm_routing_mode: str = "fallback"


class GlobalModelConfigResponse(BaseModel):
    value: GlobalModelConfig

# ── 金字塔 L0-L3 记忆 ──

class MemoryAtomCreate(BaseModel):
    """创建记忆原子的请求"""
    session_id: str
    kind: str = Field(
        default="fact",
        pattern=r"^(preference|decision|fact|constraint|goal|error_pattern|task_state|blocker)$",
    )
    title: str | None = None
    content: str = Field(..., min_length=1)
    tags: list[str] = []
    source_message_ids: list[str] = []
    confidence_score: float = 0.8
    sensitivity_level: str = Field(
        default="normal",
        pattern=r"^(normal|high)$",
    )


class MemoryAtomResponse(BaseModel):
    """记忆原子响应"""
    id: str
    session_id: str
    user_id: str
    project_key: str | None = None
    kind: str
    title: str | None = None
    content: str
    tags: list[str] = []
    source_message_ids: list[str] = []

    @field_validator("id", "session_id", "source_message_ids", mode="before")
    @classmethod
    def _stringify_uuids(cls, v):
        import uuid as _uuid
        if isinstance(v, _uuid.UUID):
            return str(v)
        if isinstance(v, list):
            return [str(x) if isinstance(x, _uuid.UUID) else x for x in v]
        return v
    confidence_score: float | None = None
    sensitivity_level: str = "normal"
    dedup_signature: str | None = None
    superseded_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MemoryScenarioResponse(BaseModel):
    """场景块响应"""
    id: str
    user_id: str
    project_key: str | None = None
    title: str
    narrative_md: str
    atom_ids: list[str] = []
    session_ids: list[str] = []
    period_start: datetime | None = None
    period_end: datetime | None = None
    created_at: datetime

    @field_validator("id", "atom_ids", "session_ids", mode="before")
    @classmethod
    def _stringify_uuids(cls, v):
        import uuid as _uuid
        if isinstance(v, _uuid.UUID):
            return str(v)
        if isinstance(v, list):
            return [str(x) if isinstance(x, _uuid.UUID) else x for x in v]
        return v

    model_config = {"from_attributes": True}


class PersonaResponse(BaseModel):
    """用户画像响应"""
    user_id: str
    profile_md: str = ""
    preferences_json: dict = {}
    scenario_ids: list[str] = []
    version: int = 1
    created_at: datetime
    updated_at: datetime

    @field_validator("scenario_ids", mode="before")
    @classmethod
    def _stringify_uuids(cls, v):
        import uuid as _uuid
        if isinstance(v, list):
            return [str(x) if isinstance(x, _uuid.UUID) else x for x in v]
        return v

    model_config = {"from_attributes": True}


class PersonaUpdateRequest(BaseModel):
    """更新用户画像请求"""
    profile_md: str | None = None
    preferences_json: dict | None = None


class CanvasUpdateRequest(BaseModel):
    """更新 Mermaid 画布请求"""
    canvas_mermaid: str | None = None


class CanvasResponse(BaseModel):
    """Mermaid 画布响应"""
    session_id: str
    canvas_mermaid: str | None = None
