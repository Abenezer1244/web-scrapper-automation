import re
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.config import settings


def _direct_url(url: str) -> str:
    """Convert Supabase PgBouncer pooler URL to direct connection.

    Supabase pooler (port 6543) uses PgBouncer in transaction mode, which
    does not support prepared statements. Switch to direct connection
    (port 5432) and let SQLAlchemy NullPool handle connections.
    """
    # pooler URL: aws-0-us-west-2.pooler.supabase.com:6543
    # direct URL: db.<project-ref>.supabase.co:5432
    if "pooler.supabase.com:6543" in url:
        # Extract project ref from the username (postgres.<project-ref>)
        m = re.search(r"postgres\.([a-z]+)@", url)
        if m:
            ref = m.group(1)
            url = re.sub(
                r"@aws-0-[a-z0-9-]+\.pooler\.supabase\.com:6543",
                f"@db.{ref}.supabase.co:5432",
                url,
            )
            # Remove the project ref from the username
            url = url.replace(f"postgres.{ref}@", "postgres@")
    return url


# ─── Async engine — FastAPI / async routes ────────────────────────────────────
# Use direct Supabase connection (not PgBouncer pooler) to avoid prepared
# statement conflicts. NullPool means we create fresh connections per request.
_async_url = _direct_url(settings.DATABASE_URL)
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
_sync_url = _direct_url(settings.DATABASE_URL_SYNC)
sync_engine = create_engine(
    _sync_url,
    poolclass=NullPool,
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
