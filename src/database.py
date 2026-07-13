"""数据库连接和会话管理"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from src.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_size=20,
    max_overflow=40,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    pool_reset_on_return="rollback",
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Nothing extra — just ensure clean state."""
    pass


@event.listens_for(engine.sync_engine, "reset")
def _reset_connection(dbapi_connection, connection_record):
    """Rollback any aborted transaction when connection is returned to pool."""
    try:
        dbapi_connection.rollback()
    except Exception:
        pass


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """获取数据库会话的依赖注入"""
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """初始化数据库（创建表和 pgvector 扩展）"""
    from src.models import Base

    async with engine.begin() as conn:
        await conn.execute(
            __import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS vector")
        )
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭数据库连接"""
    await engine.dispose()
