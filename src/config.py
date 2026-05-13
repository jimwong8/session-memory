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
    embedding_dimensions: int = 384

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
    summary_trigger_count: int = 20

    max_context_tokens: int = 8000
    context_warn_ratio: float = 0.7
    context_prepare_continuation_ratio: float = 0.85
    context_force_continuation_ratio: float = 0.95
    recent_messages_count: int = 10
    max_retrieved_messages: int = 3
    max_graph_context_ratio: float = 0.2

    grafana_password: str = "admin"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
