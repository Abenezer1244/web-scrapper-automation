from collections.abc import AsyncGenerator

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.config import settings

# ─── Async engine — FastAPI / async routes ────────────────────────────────────
# NullPool + statement_cache_size=0 for Supabase PgBouncer compatibility.
async_engine = create_async_engine(
    settings.DATABASE_URL,
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
    """FastAPI dependency: yields an async database session.

    Runs DEALLOCATE ALL at session start to clear any stale prepared
    statements left by PgBouncer's connection reuse.
    """
    async with AsyncSessionLocal() as session:
        try:
            # Clear any stale prepared statements from PgBouncer
            await session.execute(text("DEALLOCATE ALL"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─── Sync engine — Celery workers / Alembic ───────────────────────────────────
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    poolclass=NullPool,
    echo=settings.DEBUG,
)


@event.listens_for(sync_engine, "connect")
def _on_sync_connect(dbapi_connection, connection_record):
    """Clear prepared statements on new sync connections (PgBouncer compat)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("DEALLOCATE ALL")
    cursor.close()


SyncSessionLocal = sessionmaker(
    sync_engine,
    expire_on_commit=False,
)


def get_sync_db() -> Session:
    """Returns a synchronous database session for Celery workers.
    Caller is responsible for closing.
    """
    return SyncSessionLocal()
