import asyncio
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
    if settings.embedding_provider == "local":
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        embedding = await asyncio.wait_for(create_embedding("health check"), timeout=3)
        valid = isinstance(embedding, list) and len(embedding) == settings.embedding_dimensions
        if valid:
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
