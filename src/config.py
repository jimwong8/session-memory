"""应用全局配置"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Session Memory"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/session_memory"
    postgres_password: str = "postgres"
    database_echo: bool = False

    redis_url: str = "redis://redis:6379/0"
    redis_session_ttl: int = 86400
    redis_cache_ttl: int = 3600

    embedding_provider: str = "local"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dimensions: int = 1024

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    backup_openai_api_key: str = ""
    backup_openai_base_url: str = ""
    backup_openai_model: str = ""
    llm_routing_mode: str = "fallback"
    llm_route_failure_threshold: int = 3
    llm_route_cooldown_seconds: int = 300

    enable_auto_summary: bool = False
    summary_model: str = "gpt-4o-mini"
    backup_summary_model: str = ""
    tertiary_openai_model: str = "KAT-Coder-Exp-72B-1010"
    tertiary_summary_model: str = "KAT-Coder-Exp-72B-1010"
    quaternary_openai_model: str = "DeepSeek-V4-Flash"
    quaternary_summary_model: str = "DeepSeek-V4-Flash"
    summary_trigger_count: int = 20

    max_context_tokens: int = 8000
    context_warn_ratio: float = 0.7
    context_prepare_continuation_ratio: float = 0.85
    context_force_continuation_ratio: float = 0.95
    recent_messages_count: int = 10
    max_retrieved_messages: int = 3
    max_graph_context_ratio: float = 0.2

    grafana_password: str = "admin"

    # ── 金字塔 Pipeline ──
    pyramid_enabled: bool = True
    pipeline_every_n_conversations: int = 5
    pipeline_l1_idle_timeout_seconds: int = 600
    pipeline_l2_min_interval_seconds: int = 900
    pipeline_enable_warmup: bool = True
    extraction_max_atoms_per_pass: int = 20
    extraction_enable_dedup: bool = True
    extraction_dedup_cosine_threshold: float = 0.92
    persona_trigger_every_n_atoms: int = 50
    persona_trigger_cron: str = "0 3 * * 0"

    # ── 检索 ──
    recall_strategy: str = "hybrid"
    recall_timeout_ms: int = 5000
    recall_rrf_k: int = 60
    bm25_language: str = "zh"

    # ── 短期符号化（Mermaid 画布） ──
    canvas_enabled: bool = True
    canvas_min_messages: int = 20
    canvas_max_nodes: int = 15

    # ── Context Offload ──
    offload_enabled: bool = False
    offload_threshold_tokens: int = 2000
    offload_mild_ratio: float = 0.5
    offload_aggressive_ratio: float = 0.85

    # ── 卫生 ──
    capture_exclude_agents: str = "bench-judge-*,audit_*,patch_*,backfill_*,repair_*"

    # ── Markdown 同步 ──
    markdown_sync_enabled: bool = True
    markdown_sync_root: str = "/app/data/memory"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
