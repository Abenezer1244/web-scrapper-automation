from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
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
    pool_timeout=30,  # wait max 30s for a connection from the pool
    echo=settings.DEBUG,
    connect_args={
        "connect_timeout": 10,  # 10s to establish TCP connection
        # 120s max per SQL statement — prevents infinite hangs when
        # Supabase/pgbouncer stalls during commit. This was the root
        # cause of Railway workers hanging at "Checking for duplicate
        # leads" — the db.commit() would wait forever.
        "options": "-c statement_timeout=120000",
    },
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


# ─── RLS helpers for the worker sync path ───────────────────────────────────
# The async API route handlers have `get_rls_db` (src/api/deps.py) which
# sets `app.current_user_id` on the session before handing it to the
# route. The worker sync path historically opened SyncSessionLocal()
# without setting any RLS context — which means every worker-side
# query on an RLS-enabled table implicitly relied on the DB role
# having BYPASSRLS, or on the policies silently failing closed. Both
# are fragile. These two helpers make the intent explicit:
#
#   rls_sync_session(user_id) — user-scoped work (scrape a job,
#     insert results, update user quota). All queries inside the
#     block are bound to the given user_id via SET LOCAL.
#
#   system_sync_session() — system-level work that legitimately
#     needs to read/write across tenants without RLS (Celery Beat
#     canary, watchdog, bootstrap user_id lookup from a job_id,
#     reading county_connectors). The name makes the intent
#     explicit and greppable so code review catches misuse.
#
# See docs/compliance/connector-audit-2026-04-10.md and the
# full-SaaS review for the bug this fixes (H1).


@contextmanager
def rls_sync_session(user_id: str) -> Iterator[Session]:
    """Open a sync session with PostgreSQL RLS bound to a specific user.

    Issues ``SELECT set_config('app.current_user_id', :uid, true)``
    inside the session's implicit transaction so every subsequent
    query inside this block runs with the USING clause of the RLS
    policies evaluating against the given user_id.

    Caller must commit/rollback explicitly — this context manager
    only handles open/close so the existing worker code (which
    manages its own commit semantics around partial inserts) does
    not have to change shape.
    """
    session = SyncSessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        yield session
    finally:
        session.close()


@contextmanager
def system_sync_session() -> Iterator[Session]:
    """Open a sync session WITHOUT setting any RLS context.

    Use ONLY for system-level operations that legitimately need to
    read or write across tenants: the Celery Beat canary, the
    watchdog that re-queues stuck jobs, bootstrap lookups that need
    the user_id before we can enter an RLS context, and the
    scheduler tasks that iterate over county_connectors.

    Do NOT use this for per-user work. Grep for this function name
    in code review — anything inside its `with` block that touches
    a user-scoped table should have a very good reason.
    """
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_rls_role_status() -> dict:
    """Report the RLS-relevant properties of the DB connection role.

    Returns a dict with:
      - role: the Postgres role name
      - bypassrls: True if the role bypasses every RLS policy
      - is_superuser: True if the role is a Postgres superuser
        (superusers also implicitly bypass RLS)

    Called at app startup (FastAPI lifespan + Celery worker_ready)
    to make the security posture visible in logs. If bypassrls is
    True, the RLS policies defined in the migrations are effectively
    decorative — the app's application-level WHERE user_id filters
    are the only tenant boundary. This function does NOT raise; it
    returns the facts so the caller can log a warning and the ops
    team can plan a role downgrade later. C2 from the full-SaaS
    review.
    """
    import logging

    log = logging.getLogger("security.rls")
    try:
        with sync_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT current_user, rolbypassrls, rolsuper "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).fetchone()
            if row is None:
                log.error("RLS status check: could not resolve current_user")
                return {"role": None, "bypassrls": None, "is_superuser": None}
            role, bypassrls, is_super = row[0], bool(row[1]), bool(row[2])
            effective_bypass = bypassrls or is_super
            if effective_bypass:
                log.error(
                    "RLS advisory: DB role '%s' has BYPASSRLS=%s SUPERUSER=%s — "
                    "row-level security policies are NOT enforced. Tenant "
                    "isolation relies on application WHERE filters only. "
                    "Plan a role downgrade to remove implicit bypass before "
                    "scaling multi-tenant traffic.",
                    role, bypassrls, is_super,
                )
            else:
                log.info(
                    "RLS advisory: DB role '%s' does not bypass RLS — "
                    "policies are active.", role,
                )
            return {
                "role": role,
                "bypassrls": bypassrls,
                "is_superuser": is_super,
            }
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("RLS status check failed: %s", exc)
        return {"role": None, "bypassrls": None, "is_superuser": None}
