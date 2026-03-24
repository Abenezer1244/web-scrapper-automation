from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from src.config import settings

# ─── Async engine — FastAPI / async routes ────────────────────────────────────
# Supabase port 6543 = pgbouncer (breaks asyncpg prepared statements).
# Force port 5432 (direct connection) for asyncpg compatibility.
_async_url = settings.DATABASE_URL.replace(":6543/", ":5432/")

async_engine = create_async_engine(
    _async_url,
    poolclass=NullPool,
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
# Route through pgbouncer (port 6543) to avoid MaxClientsInSessionMode errors
# when 4+ Celery workers open direct connections. psycopg2 works fine with
# pgbouncer transaction mode (unlike asyncpg which needs direct for prepared stmts).
#
# Enterprise reliability:
# - pool_pre_ping: test connection before use, auto-reconnect if dead
# - pool_recycle: close connections older than 5 min (pgbouncer kills idle at ~30s)
# - pool_size: 2 per worker (scrape + status update)
# - max_overflow: 3 extra connections for burst DB activity
_sync_url = settings.DATABASE_URL_SYNC.replace(":5432/", ":6543/")

sync_engine = create_engine(
    _sync_url,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=2,
    max_overflow=3,
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
