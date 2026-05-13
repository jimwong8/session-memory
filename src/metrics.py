"""Prometheus 指标定义"""

from prometheus_client import Counter, Histogram

AUTO_MESSAGE_DEDUPE_HITS = Counter(
    "auto_message_dedupe_hits_total",
    "Auto message dedupe hits",
    ["path"],
)

AUTO_MESSAGE_DEDUPE_CONFLICTS = Counter(
    "auto_message_dedupe_conflicts_total",
    "Auto message dedupe conflicts",
    ["path"],
)

SESSIONS_TOTAL = Counter(
    "sessions_total",
    "Total sessions created",
)

MESSAGES_TOTAL = Counter(
    "messages_total",
    "Total messages added",
)

EMBEDDING_SUCCESS_COUNT = Counter(
    "embedding_generate_success_total",
    "Total successful embedding generations",
)

EMBEDDING_FAIL_COUNT = Counter(
    "embedding_generate_fail_total",
    "Total failed embedding generations",
)

EMBEDDING_BACKFILL_PROCESSED = Counter(
    "embedding_backfill_processed_total",
    "Total embedding backfill records processed",
    ["result"],
)

EMBEDDING_DURATION = Histogram(
    "embedding_generate_duration_seconds",
    "Embedding generation duration in seconds",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

MESSAGE_SIZE_BYTES = Histogram(
    "message_size_bytes",
    "Message content size distribution in bytes",
    buckets=[100, 500, 1024, 2048, 5120, 10240, 20480, 51200],
)

COMPRESSION_RATIO = Histogram(
    "message_compression_ratio",
    "Message compression ratio (compressed_size / original_size)",
    buckets=[0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0],
)

COMPRESSION_APPLIED = Counter(
    "message_compression_applied_total",
    "Total messages with compression applied",
    ["strategy"],
)

SUMMARY_SUCCESS_COUNT = Counter(
    "summary_generate_success_total",
    "Total successful summary generations",
)

SUMMARY_FAIL_COUNT = Counter(
    "summary_generate_fail_total",
    "Total failed summary generations",
)

SUMMARY_DURATION = Histogram(
    "summary_generate_duration_seconds",
    "Summary generation duration in seconds",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)


CONTEXT_WINDOW_TOKENS = Histogram(
    "context_window_tokens",
    "Total context window tokens before sending to LLM",
    buckets=[256, 512, 1024, 2048, 4096, 8192, 16384],
)

CONTEXT_BUDGET_RATIO = Histogram(
    "context_budget_ratio",
    "Context token budget utilization ratio",
    buckets=[0.1, 0.25, 0.5, 0.7, 0.85, 0.95, 1.0],
)

CONTEXT_BUDGET_STATE = Counter(
    "context_budget_state_total",
    "Context budget state counts",
    ["state"],
)


CONTINUATION_TRIGGER_TOTAL = Counter(
    "continuation_trigger_total",
    "Total continuation payloads triggered",
    ["failure_code"],
)

CONTINUATION_CONSUMED_TOTAL = Counter(
    "continuation_consumed_total",
    "Total consumed continuation payloads",
)

CONTINUATION_PENDING_GAUGE = Histogram(
    "continuation_pending_count",
    "Continuation pending count snapshot",
    buckets=[0, 1, 2, 5, 10, 20, 50],
)

CONTINUATION_LAST_AGE_SECONDS = Histogram(
    "continuation_last_age_seconds",
    "Age of latest continuation payload in seconds",
    buckets=[60, 300, 900, 3600, 21600, 86400, 172800],
)


CONTINUATION_FAILURE_TOTAL = Counter(
    "continuation_failure_total",
    "Total continuation failures by failure code",
    ["failure_code"],
)

CONTINUATION_RECOVERY_LATENCY_SECONDS = Histogram(
    "continuation_recovery_latency_seconds",
    "Continuation recovery latency in seconds",
    buckets=[1, 5, 10, 30, 60, 300, 900, 3600],
)


LLM_ROUTE_HIT_TOTAL = Counter(
    "llm_route_hit_total",
    "Total successful hits by LLM route",
    ["operation", "route"],
)

LLM_ROUTE_FAIL_TOTAL = Counter(
    "llm_route_fail_total",
    "Total failures by LLM route",
    ["operation", "route"],
)

LLM_ROUTE_FALLBACK_TOTAL = Counter(
    "llm_route_fallback_total",
    "Total fallback transitions between LLM routes",
    ["operation"],
)

LLM_ROUTE_CIRCUIT_OPEN_TOTAL = Counter(
    "llm_route_circuit_open_total",
    "Total times a route was skipped due to open circuit",
    ["operation", "route"],
)
