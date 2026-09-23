import asyncio
import os
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import time
from pathlib import Path
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sql_text


from src.database import init_db, async_session
from src.cache import cache
from src.config import settings
from src.embedding_service import create_embedding, warmup_embedding
from src.routes import router
from src.routes_proactive import router as proactive_router

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

REQUEST_COUNT = Counter('http_requests_total', 'Total HTTP requests', ['method', 'endpoint', 'status'])
REQUEST_DURATION = Histogram('http_request_duration_seconds', 'HTTP request duration', ['method', 'endpoint'])

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
APP_DIR = STATIC_DIR / "app"
APP_INDEX_FILE = APP_DIR / "index.html"

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化数据库...")
    await init_db()
    logger.info("正在连接 Redis...")
    await cache.connect()
    if settings.embedding_provider == "local" and os.environ.get("ENABLE_EMBEDDING_WARMUP", "true").lower() == "true":
        try:
            logger.info("正在预热本地嵌入模型...")
            await warmup_embedding()
            logger.info("本地嵌入模型预热完成")
        except Exception as exc:
            logger.warning(f"本地嵌入模型预热失败，将按懒加载继续运行: {exc}")
    logger.info("应用启动完成 ✓")
    try:
        yield
    finally:
        logger.info("正在关闭连接...")
        await cache.close()
        logger.info("应用已关闭 ✓")

app = FastAPI(
    title="Session Memory API",
    description="会话记忆管理服务",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/api/v1/stats")
async def system_stats():
    """System stats - cached for 60s, auto-refresh if empty"""
    if _stats_cache["messages"] == 0:
        await _do_refresh()
    return _stats_cache
    return _stats_cache

_stats_cache = {"messages": 0, "sessions": 0, "entities": 0, "relations": 0,
                 "atoms": 0, "scenarios": 0, "bge": 0, "m3": 0, "nv": 0,
                 "bge_pct": 0, "m3_pct": 0, "nv_pct": 0, "kg_done": 0, "kg_pending": 0, "kg_failed": 0}

async def _do_refresh():
    """Refresh stats cache"""
    global _stats_cache
    from src.database import async_session
    from sqlalchemy import text
    async with async_session() as db:
        # pg_stat_user_tables.n_live_tup is UNRELIABLE here: autovacuum has never
        # run on these tables (last_autovacuum IS NULL), so n_live_tup reported
        # 957 for a 4.27M-row messages table -> percentages blew past 60000%.
        # Use pg_class.reltuples (updated by ANALYZE) and fall back to exact
        # count(*) whenever the estimate is obviously broken.
        rows = (await db.execute(text("""
            SELECT c.relname, GREATEST(c.reltuples::bigint, 0)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname IN ('messages','sessions','kg_entities','kg_relations','memory_atoms','memory_scenarios')
        """))).fetchall()
        est = {r[0]: int(r[1] or 0) for r in rows}

        # Every headline number is counted exactly. reltuples drifts by tens of
        # thousands between ANALYZE runs (sessions was off by ~44k, kg_entities
        # by ~5k), and this endpoint is cached for 60s, so exact counts are
        # affordable and keep /stats consistent with /admin/dashboard.
        for tbl in ('messages', 'sessions', 'kg_entities', 'kg_relations',
                    'memory_atoms', 'memory_scenarios'):
            try:
                est[tbl] = (await db.execute(text(f"SELECT count(*) FROM {tbl}"))).scalar() or 0
            except Exception:
                # ⚠ except 吞掉异常后必须 rollback：否则同一 session 被毒化，
                #    后续任何 db 查询都会 PendingRollbackError（偶发 500，极难定位）。
                try:
                    await db.rollback()
                except Exception:
                    pass
                est[tbl] = est.get(tbl, 0)
        msgs = est.get('messages', 0)
        # Use partial-index-friendly counts (fast) for bge/m3; read KG progress from
        # the kg_jobs table (real status), not a messages-table estimate — the old
        # estimate made kg_done a constant 0 (dashboard "false zero" bug).
        bge = (await db.execute(text("SELECT count(*) FROM messages WHERE embedding IS NOT NULL"))).scalar() or 0
        m3 = (await db.execute(text("SELECT count(*) FROM messages WHERE embedding_m3 IS NOT NULL"))).scalar() or 0
        nv = (await db.execute(text("SELECT count(*) FROM messages WHERE embedding_nv IS NOT NULL"))).scalar() or 0
        kg_rows = (await db.execute(text("SELECT status, count(*) FROM kg_jobs GROUP BY status"))).fetchall()
        kg_counts = {r[0]: r[1] for r in kg_rows}
        kg_done = kg_counts.get('completed', 0)
        kg_pending = kg_counts.get('pending', 0)
        kg_failed = kg_counts.get('failed', 0)
        
        _stats_cache = {
            "messages": msgs, "sessions": est.get('sessions', 0),
            "entities": est.get('kg_entities', 0), "relations": est.get('kg_relations', 0),
            "atoms": est.get('memory_atoms', 0), "scenarios": est.get('memory_scenarios', 0),
            "bge": bge, "m3": m3, "nv": nv,
            "bge_pct": round(bge/max(msgs,1)*100,1), "m3_pct": round(m3/max(msgs,1)*100,1),
            "nv_pct": round(nv/max(msgs,1)*100,1),
            "kg_done": kg_done, "kg_pending": kg_pending, "kg_failed": kg_failed,
        }

@app.get("/api/v1/stats/refresh")
async def trigger_stats_refresh():
    """Force stats refresh"""
    await _do_refresh()
    return {"status": "refreshed", "stats": _stats_cache}

@app.on_event("startup")
async def start_stats_refresh():
    """Background task to refresh stats every 60s"""
    import asyncio
    
    async def refresh():
        while True:
            try:
                await _do_refresh()
            except Exception as e:
                print(f"Stats refresh error: {e}")
            await asyncio.sleep(60)
    
    asyncio.create_task(refresh())

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    endpoint = request.url.path
    method = request.method
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        duration = time.perf_counter() - start
        REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=status).inc()
        REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)

app.include_router(router)
app.include_router(proactive_router)

@app.get("/", include_in_schema=False)
async def ui_index():
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    return {"status": "ok", "service": "session-memory", "ui": "not-built"}

@app.get("/app", include_in_schema=False)
async def app_index():
    if APP_INDEX_FILE.exists():
        return FileResponse(APP_INDEX_FILE)
    return {"status": "ok", "service": "session-memory", "ui": "app-not-built"}

@app.get("/app/{path:path}", include_in_schema=False)
async def app_index_fallback(path: str):
    if APP_INDEX_FILE.exists():
        return FileResponse(APP_INDEX_FILE)
    return {"status": "ok", "service": "session-memory", "ui": "app-not-built", "path": path}

@app.get("/health")
async def health_check(deep: bool = Query(False)):
    if not deep:
        return {
            "status": "ok",
            "service": "session-memory",
            "mode": "shallow",
        }

    checks: dict[str, dict] = {}
    overall_status = "ok"

    try:
        async with async_session() as session:
            await session.execute(sql_text("SELECT 1"))
        checks["postgres"] = {"status": "ok"}
    except Exception as exc:
        overall_status = "degraded"
        checks["postgres"] = {"status": "error", "error": str(exc)}

    try:
        await cache.client.ping()
        checks["redis"] = {"status": "ok"}
    except Exception as exc:
        overall_status = "degraded"
        checks["redis"] = {"status": "error", "error": str(exc)}

    try:
        embedding = await asyncio.wait_for(create_embedding("health check"), timeout=10)
        if embedding is None:
            checks["embedding"] = {
                "status": "skipped",
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "reason": "embedding skipped (load protection)",
            }
            valid = False
        else:
            valid = isinstance(embedding, list) and len(embedding) == settings.embedding_dimensions
        if embedding is None:
            pass  # already marked skipped above
        elif valid:
            checks["embedding"] = {
                "status": "ok",
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dimensions": len(embedding),
            }
        else:
            overall_status = "degraded"
            checks["embedding"] = {
                "status": "error",
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dimensions": len(embedding) if isinstance(embedding, list) else None,
                "error": "embedding dimensions mismatch",
            }
    except Exception as exc:
        overall_status = "degraded"
        checks["embedding"] = {
            "status": "error",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "error": str(exc),
        }

    return {
        "status": overall_status,
        "service": "session-memory",
        "mode": "deep",
        "checks": checks,
    }

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
