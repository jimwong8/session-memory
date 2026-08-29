"""审计报告聚合服务 - 读取 logs/audits/*_latest.json 并聚合成 Dashboard 响应"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.database import async_session
from src.cache import cache
from src.metrics import CONTINUATION_CONSUMED_TOTAL, CONTINUATION_LAST_AGE_SECONDS, CONTINUATION_PENDING_GAUGE, CONTINUATION_TRIGGER_TOTAL, CONTINUATION_FAILURE_TOTAL, CONTINUATION_RECOVERY_LATENCY_SECONDS

from src.schemas import (
    AdminDashboardResponse,
    AuditAlertsSummary,
    AuditCapacitySummary,
    AuditContinuationSummary,
    AuditDuplicatesSummary,
    AuditHealthSummary,
    AuditKGSummary,
    AuditSectionBase,
    AuditSharedMemorySummary,
    SharedMemoryProjectSample,
    AuditTrendsEntry,
    AuditTrendsSummary,
    KGOpsSummary,
    KGOpsErrorStat,
    KGOpsWorkerStat,
    ContinuationOpsSummary,
    LLMRouteMetricsSummary,
    OperationHistoryItem,
    AIOpsAdviceAction,
    AIOpsAdviceSummary,
    OpenCodeFlaggedSession,
    OpenCodeRepeatOffender,
    OpenCodeRepeatOffenderAction,
    OpenCodeRuntimeAuditSummary,
)

logger = logging.getLogger(__name__)

AUDIT_DIR = Path("/app/logs/audits")


def _read_json_ignore_errors(path: Path) -> dict[str, Any] | None:
    """安全读取 JSON 文件，文件不存在或解析失败时返回 None 并记录警告。"""
    if not path.exists():
        logger.warning("audit report file not found: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("failed to parse %s: %s", path, exc)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _severity_from_summary(summary_json: dict | None) -> str:
    """从 audit_summary_latest.json 的 overall 推断综合状态。"""
    if not summary_json:
        return "unknown"
    checks = summary_json.get("generated_files", [])
    # EXCLUDE external dependency checks (SSH/network not core health)
    _EXCLUDED = {"audit_opencode_runtime", "audit_ai_ops"}
    checks = [c for c in checks if c.get("name") not in _EXCLUDED]
    if any(c.get("status") == "critical" for c in checks):
        return "critical"
    if any(c.get("status") == "warning" for c in checks):
        return "warning"
    return "ok"


def _build_health(data: dict | None) -> AuditHealthSummary:
    if data is None:
        return AuditHealthSummary(status="unknown", issues=[], generated_at=_now_iso())
    return AuditHealthSummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        service_health=data.get("service_health"),
        db_counts=data.get("db_counts"),
        continuation_latest=data.get("continuation_latest"),
    )


def _build_continuation(data: dict | None) -> AuditContinuationSummary:
    if data is None:
        return AuditContinuationSummary(status="unknown", issues=[], generated_at=_now_iso())
    latest = data.get("latest") or {}
    status = data.get("status", "unknown")
    issues = data.get("issues", [])
    if not latest and status in {"degraded", "unknown"}:
        status = "idle"
        issues = []
    return AuditContinuationSummary(
        status=status,
        issues=issues,
        generated_at=data.get("generated_at"),
        source_session_id=latest.get("source_session_id"),
        failure_code=latest.get("failure_code"),
        consumed_by_session_id=latest.get("consumed_by_session_id"),
    )


def _build_duplicates(data: dict | None) -> AuditDuplicatesSummary:
    if data is None:
        return AuditDuplicatesSummary(status="unknown", issues=[], generated_at=_now_iso())
    return AuditDuplicatesSummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        duplicate_group_count=data.get("duplicate_group_count", 0),
    )


def _build_kg(data: dict | None) -> AuditKGSummary:
    if data is None:
        return AuditKGSummary(status="unknown", issues=[], generated_at=_now_iso())
    return AuditKGSummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        kg_fail_json_extract=data.get("kg_fail_json_extract", 0),
        kg_fail_429=data.get("kg_fail_429", 0),
        kg_success=data.get("kg_success", 0),
        kg_pending=data.get("kg_pending", 0),
        kg_failed=data.get("kg_failed", 0),
        kg_deadletter=data.get("kg_deadletter", 0),
        kg_running=data.get("kg_running", 0),
        source=data.get("source"),
    )


def _build_capacity(data: dict | None) -> AuditCapacitySummary:
    if data is None:
        return AuditCapacitySummary(
            status="unknown", issues=[],
            generated_at=_now_iso(),
            total_messages=0, active_sessions=0,
            kg_jobs_pending=0, kg_jobs_deadletter=0,
            kg_jobs_oldest_pending_age_seconds=0, alert_severity="unknown",
        )
    return AuditCapacitySummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        total_messages=data.get("total_messages", 0),
        active_sessions=data.get("active_sessions", 0),
        kg_jobs_pending=data.get("kg_jobs_pending", 0),
        kg_jobs_deadletter=data.get("kg_jobs_deadletter", 0),
        kg_jobs_oldest_pending_age_seconds=data.get("kg_jobs_oldest_pending_age_seconds", 0),
        alert_severity=data.get("alert_severity", "ok"),
        embed_pct=float(data.get("embed_pct") or 0),
        m3_pct=float(data.get("m3_pct") or 0),
        embed_done=data.get("embed_done", 0),
        m3_done=data.get("m3_done", 0),
        kg_jobs_completed=data.get("kg_jobs_completed", 0),
        kg_jobs_failed=data.get("kg_jobs_failed", 0),
    )


def _build_alerts(data: dict | None) -> AuditAlertsSummary:
    if data is None:
        return AuditAlertsSummary(
            status="unknown", issues=[], generated_at=_now_iso(), checks_evaluated=0
        )
    # issues 字段在真实数据中是 list[dict]，直接透传
    raw_issues: list[Any] = data.get("issues", [])
    issues: list[dict] = [i for i in raw_issues if isinstance(i, dict)]
    return AuditAlertsSummary(
        status=data.get("severity", "ok"),
        issues=issues,
        generated_at=data.get("generated_at"),
        checks_evaluated=data.get("checks_evaluated", 0),
    )


def _build_trends(data: dict | None) -> AuditTrendsSummary:
    if data is None:
        return AuditTrendsSummary(window_size=0, trends={})
    raw_trends: dict[str, list[dict]] = data.get("trends", {})
    parsed: dict[str, list[AuditTrendsEntry]] = {}
    for key, entries in raw_trends.items():
        parsed[key] = [
            AuditTrendsEntry(
                file=e.get("file", ""),
                status=e.get("status", "unknown"),
                issues=e.get("issues", []),
            )
            for e in entries
        ]
    return AuditTrendsSummary(
        window_size=data.get("window_size", 0),
        trends=parsed,
    )


async def _build_continuation_ui_ops() -> ContinuationOpsSummary:
    latest = await cache.get_plugin_state("continuation:latest")
    if not latest:
        return ContinuationOpsSummary()
    consumed_by = latest.get("consumed_by_session_id")
    return ContinuationOpsSummary(
        auto_continued_count=1 if consumed_by else 0,
        last_consumed_exists=bool(consumed_by),
        last_consumed_target_session_id=consumed_by,
        latest_source_session_id=latest.get("source_session_id"),
        latest_consumed_by_session_id=consumed_by,
    )


async def _build_continuation_ops() -> dict:
    latest = await cache.get_plugin_state("continuation:latest")
    if not latest:
        return {
            "pending": 0,
            "has_latest": False,
            "latest_age_seconds": None,
            "failure_code": None,
            "consumed": 0,
        }

    created_at = latest.get("created_at")
    latest_age_seconds = None
    if created_at:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            latest_age_seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            latest_age_seconds = None

    pending = 0 if latest.get("consumed_by_session_id") else 1
    consumed = 1 if latest.get("consumed_by_session_id") else 0
    failure_code = latest.get("failure_code")
    if failure_code:
        CONTINUATION_TRIGGER_TOTAL.labels(failure_code=failure_code).inc(0)
        CONTINUATION_FAILURE_TOTAL.labels(failure_code=failure_code).inc(0)
    CONTINUATION_PENDING_GAUGE.observe(pending)
    if latest_age_seconds is not None:
        CONTINUATION_LAST_AGE_SECONDS.observe(latest_age_seconds)
    if consumed:
        CONTINUATION_CONSUMED_TOTAL.inc(0)
        if latest_age_seconds is not None:
            CONTINUATION_RECOVERY_LATENCY_SECONDS.observe(latest_age_seconds)

    return {
        "pending": pending,
        "has_latest": True,
        "latest_age_seconds": latest_age_seconds,
        "failure_code": failure_code,
        "consumed": consumed,
    }


def _parse_prom_counter_lines(metrics_text: str, metric_name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(metric_name + '{'):
            continue
        try:
            left, value = line.rsplit(' ', 1)
            label_text = left[left.find('{') + 1:left.rfind('}')]
            labels = {}
            for part in label_text.split(','):
                if '=' not in part:
                    continue
                k, v = part.split('=', 1)
                labels[k.strip()] = v.strip().strip('"')
            operation = labels.get('operation', 'unknown')
            route = labels.get('route', 'unknown')
            result[f'{operation}:{route}'] = int(float(value))
        except Exception:
            continue
    return result


async def _build_llm_route_metrics() -> LLMRouteMetricsSummary:
    try:
        hit_counts = await cache.get_llm_route_metric_map('hit')
        fail_counts = await cache.get_llm_route_metric_map('fail')
        fallback_counts = await cache.get_llm_route_metric_map('fallback')
        circuit_open_counts = await cache.get_llm_route_metric_map('circuit_open')
        window_hit_counts = await cache.get_llm_route_window_metric_map('hit')
        window_fail_counts = await cache.get_llm_route_window_metric_map('fail')
        window_fallback_counts = await cache.get_llm_route_window_metric_map('fallback')
        window_circuit_open_counts = await cache.get_llm_route_window_metric_map('circuit_open')
        return LLMRouteMetricsSummary(
            hit_counts=hit_counts,
            fail_counts=fail_counts,
            fallback_counts=fallback_counts,
            circuit_open_counts=circuit_open_counts,
            window_hit_counts=window_hit_counts,
            window_fail_counts=window_fail_counts,
            window_fallback_counts=window_fallback_counts,
            window_circuit_open_counts=window_circuit_open_counts,
        )
    except Exception:
        return LLMRouteMetricsSummary()


async def _build_operation_history() -> list[OperationHistoryItem]:
    async with async_session() as db:
        rows = list((await db.execute(text("select action, target_type, target_id, status, message, operator, created_at from operation_history order by created_at desc limit 10"))).all())
    return [OperationHistoryItem(action=row[0], target_type=row[1], target_id=row[2], status=row[3], message=row[4], operator=row[5], created_at=row[6].isoformat() if row[6] else None) for row in rows]


async def _build_kg_ops() -> KGOpsSummary:
    async with async_session() as db:
        status_rows = list((await db.execute(text("select status, count(*) as count from kg_jobs group by status"))).all())
        pending_ready_rows = list((await db.execute(text("select count(*) from kg_jobs where status = 'pending' and coalesce(last_error, '<none>') = '<none>'"))).all())
        retry_wait_rows = list((await db.execute(text("select count(*) from kg_jobs where status = 'pending' and coalesce(last_error, '<none>') <> '<none>'"))).all())
        cooldown_running_rows = list((await db.execute(text("select count(*) from kg_jobs where status = 'running' and coalesce(last_error, '<none>') like 'rate_limited%'"))).all())
        reason_rows = list((await db.execute(text("select coalesce(last_error, '<none>') as reason, count(*) as count from kg_jobs where status = 'dead_letter' group by coalesce(last_error, '<none>') order by count(*) desc limit 10"))).all())
        pending_reason_rows = list((await db.execute(text("select coalesce(last_error, '<none>') as reason, count(*) as count from kg_jobs where status = 'pending' group by coalesce(last_error, '<none>') order by count(*) desc limit 10"))).all())
        worker_rows = list((await db.execute(text("select coalesce(locked_by, '<none>') as worker_id, count(*) as count from kg_jobs where status = 'running' group by coalesce(locked_by, '<none>') order by count(*) desc limit 10"))).all())

    counters = {row[0]: int(row[1]) for row in status_rows}
    return KGOpsSummary(
        deadletter_reasons=[KGOpsErrorStat(reason=row[0], count=int(row[1])) for row in reason_rows],
        pending_reasons=[KGOpsErrorStat(reason=row[0], count=int(row[1])) for row in pending_reason_rows],
        running_workers=[KGOpsWorkerStat(worker_id=row[0], count=int(row[1])) for row in worker_rows],
        pending_total=counters.get('pending', 0),
        queue_ready_total=int(pending_ready_rows[0][0]) if pending_ready_rows else 0,
        queue_retry_wait_total=int(retry_wait_rows[0][0]) if retry_wait_rows else 0,
        queue_cooldown_running_total=int(cooldown_running_rows[0][0]) if cooldown_running_rows else 0,
        running_total=counters.get('running', 0),
        succeeded_total=counters.get('succeeded', 0),
        deadletter_total=counters.get('dead_letter', 0),
    )


def _build_shared_memory(data: dict | None) -> AuditSharedMemorySummary:
    if data is None:
        return AuditSharedMemorySummary(
            status="unknown", issues=[], generated_at=_now_iso(),
            project_session_count=0, distinct_project_count=0,
            summary_sources=0, kg_entity_sources=0,
        )
    return AuditSharedMemorySummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        project_session_count=data.get("project_session_count", 0),
        distinct_project_count=data.get("distinct_project_count", 0),
        summary_sources=data.get("summary_sources", 0),
        kg_entity_sources=data.get("kg_entity_sources", 0),
        project_samples=[SharedMemoryProjectSample(**sample) for sample in data.get("project_samples", []) if isinstance(sample, dict)],
    )


def _build_ai_ops_advice(data: dict | None) -> AIOpsAdviceSummary:
    if data is None:
        return AIOpsAdviceSummary(status="unknown", issues=[], generated_at=_now_iso(), summary="AI 运维解释尚未生成。")
    raw_actions = data.get("recommended_actions", [])
    actions = [AIOpsAdviceAction(**item) for item in raw_actions if isinstance(item, dict) and item.get("action")]
    issues = []
    if data.get("error"):
        issues.append(str(data.get("error")))
    return AIOpsAdviceSummary(
        status=data.get("status", "unknown"),
        issues=issues,
        generated_at=data.get("generated_at"),
        summary=data.get("summary", ""),
        risk_level=data.get("risk_level", "unknown"),
        recommended_actions=actions,
        model_used=data.get("model_used"),
        error=data.get("error"),
    )


def _build_opencode_runtime(data: dict | None) -> OpenCodeRuntimeAuditSummary:
    if data is None:
        return OpenCodeRuntimeAuditSummary(status="unknown", issues=[], generated_at=_now_iso())
    return OpenCodeRuntimeAuditSummary(
        status=data.get("status", "unknown"),
        issues=data.get("issues", []),
        generated_at=data.get("generated_at"),
        flagged_sessions=[OpenCodeFlaggedSession(**item) for item in data.get("flagged_sessions", []) if isinstance(item, dict)],
        repeat_offender_sessions=[OpenCodeRepeatOffender(**item) for item in data.get("repeat_offender_sessions", []) if isinstance(item, dict) and item.get("session_id")],
        repeat_offender_actions=[OpenCodeRepeatOffenderAction(**item) for item in data.get("repeat_offender_actions", []) if isinstance(item, dict) and item.get("session_id")],
        sqlite=data.get("sqlite"),
        plugin_state=data.get("plugin_state"),
        logs=data.get("logs"),
        error_classes=data.get("error_classes", {}),
    )






def _build_bridge_status(data: dict | None) -> dict:
    if not data:
        return {}
    return {
        "status": data.get("status"),
        "collector": data.get("collector"),
        "spool": data.get("spool"),
        "sessions": data.get("sessions"),
        "issues": data.get("issues", []),
    }
async def get_admin_dashboard() -> AdminDashboardResponse:
    """读取所有 *_latest.json 并聚合成 AdminDashboardResponse。"""
    summary_data = _read_json_ignore_errors(AUDIT_DIR / "audit_summary_latest.json")
    health_data = _read_json_ignore_errors(AUDIT_DIR / "audit_health_latest.json")
    continuation_data = _read_json_ignore_errors(AUDIT_DIR / "audit_continuation_latest.json")
    duplicates_data = _read_json_ignore_errors(AUDIT_DIR / "audit_duplicates_latest.json")
    kg_data = _read_json_ignore_errors(AUDIT_DIR / "audit_kg_latest.json")
    capacity_data = _read_json_ignore_errors(AUDIT_DIR / "audit_capacity_latest.json")
    alerts_data = _read_json_ignore_errors(AUDIT_DIR / "audit_alerts_latest.json")
    trends_data = _read_json_ignore_errors(AUDIT_DIR / "audit_trends_latest.json")
    shared_memory_data = _read_json_ignore_errors(AUDIT_DIR / "audit_shared_memory_latest.json")
    ai_ops_data = _read_json_ignore_errors(AUDIT_DIR / "audit_ai_ops_latest.json")
    opencode_runtime_data = _read_json_ignore_errors(AUDIT_DIR / "audit_opencode_runtime_latest.json")
    bridge_data = _read_json_ignore_errors(AUDIT_DIR / "audit_bridge_latest.json")
    raw_events_data = _read_json_ignore_errors(AUDIT_DIR / "audit_raw_events_latest.json")

    overall = _severity_from_summary(summary_data)

    kg_ops = await _build_kg_ops()
    continuation_metrics = await _build_continuation_ops()
    continuation_ui_ops = await _build_continuation_ui_ops()
    operation_history = await _build_operation_history()
    llm_route_metrics = await _build_llm_route_metrics()

    return AdminDashboardResponse(
        generated_at=_now_iso(),
        overall_status=overall,
        health=_build_health(health_data),
        continuation=_build_continuation(continuation_data),
        duplicates=_build_duplicates(duplicates_data),
        kg=_build_kg(kg_data),
        capacity_snapshot=_build_capacity(capacity_data),
        alerts=_build_alerts(alerts_data),
        trends=_build_trends(trends_data),
        shared_memory=_build_shared_memory(shared_memory_data),
        kg_ops=kg_ops,
        continuation_metrics=continuation_metrics,
        continuation_ops=continuation_ui_ops,
        llm_route_metrics=llm_route_metrics,
        operation_history=operation_history,
        ai_ops_advice=_build_ai_ops_advice(ai_ops_data),
        opencode_runtime=_build_opencode_runtime(opencode_runtime_data),
        raw_events=_build_bridge_status(raw_events_data),
        bridge=_build_bridge_status(bridge_data),
    )
