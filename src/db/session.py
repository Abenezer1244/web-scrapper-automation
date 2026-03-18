from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings

# ─── Async engine — FastAPI / async routes ────────────────────────────────────
async_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.DEBUG,
    connect_args={"statement_cache_size": 0},
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─── Sync engine — Celery workers / Alembic ───────────────────────────────────
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.DEBUG,
)

SyncSessionLocal = sessionmaker(
    sync_engine,
    expire_on_commit=False,
)


def get_sync_db() -> Session:
    """Returns a synchronous database session for Celery workers.
    Caller is responsible for closing.
    """
    return SyncSessionLocal()
