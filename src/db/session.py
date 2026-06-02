from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

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


# ─── Per-transaction RLS GUC reapply (Phase 1 of the RLS cutover) ───────────
# ``set_config('app.current_user_id', uid, is_local=true)`` is TRANSACTION
# scoped — it is cleared on every COMMIT/ROLLBACK. run_scrape_job commits
# mid-task, so the GUC vanishes and the NEXT transaction on the same session
# runs with no user context. Today that is masked because the prod role has
# BYPASSRLS; under the non-BYPASSRLS cutover role it would make RLS block
# every user-scoped query after the first commit.
#
# This after_begin listener re-binds the GUC at the start of EVERY transaction,
# reading the user_id off ``session.info`` so it survives commits. It is gated
# on ``session.info['rls_user_id']`` so sessions that intentionally carry no
# user context (system_sync_session, plain get_db) are untouched — the handler
# returns immediately for them. set_config(local=true) self-clears at tx end,
# so no GUC leaks across pooled-connection reuse.
_RLS_GUC_SQL = text("SELECT set_config('app.current_user_id', :uid, true)")


@event.listens_for(Session, "after_begin")
def _reapply_rls_guc(session: Session, transaction, connection) -> None:
    uid = session.info.get("rls_user_id")
    if uid is None:
        return
    connection.execute(_RLS_GUC_SQL, {"uid": uid})


@contextmanager
def rls_sync_session(user_id: str) -> Iterator[Session]:
    """Open a sync session with PostgreSQL RLS bound to a specific user.

    Stores ``user_id`` on ``session.info['rls_user_id']`` so the
    ``after_begin`` listener re-applies ``app.current_user_id`` on EVERY
    transaction — surviving the mid-task commits run_scrape_job performs.
    The explicit set_config below covers the very first transaction
    immediately (belt-and-suspenders; the listener also fires for it).

    Caller must commit/rollback explicitly — this context manager
    only handles open/close so the existing worker code (which
    manages its own commit semantics around partial inserts) does
    not have to change shape.
    """
    session = SyncSessionLocal()
    session.info["rls_user_id"] = str(user_id)
    try:
        session.execute(_RLS_GUC_SQL, {"uid": str(user_id)})
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


def _role_status(engine) -> dict | None:
    """Return {role, bypassrls, is_superuser} for the role ``engine`` connects as.

    None if ``current_user`` cannot be resolved. Shared by check_rls_role_status
    so it can inspect BOTH the sync engine (workers/alembic, DATABASE_URL_SYNC)
    and the async engine (FastAPI request traffic, DATABASE_URL).
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolbypassrls, rolsuper "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
    if row is None:
        return None
    return {"role": row[0], "bypassrls": bool(row[1]), "is_superuser": bool(row[2])}


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
    are the only tenant boundary.

    REDTEAM HIGH T2 (docs/security/REDTEAM-2026-06-01.md): in
    production this is no longer advisory. If the runtime role
    bypasses RLS (BYPASSRLS or superuser), the WITH CHECK write
    policies added in migration 025 are silently skipped and the
    per-query user_id filter becomes the *only* tenant boundary —
    one missing WHERE clause leaks across tenants. So when
    ENVIRONMENT == "production" and the role carries an implicit
    bypass, this function raises RuntimeError to refuse boot. Outside
    production it stays advisory (logs only) so local/dev databases —
    where the connecting role is often the superuser/owner — still
    start. C2 from the full-SaaS review.
    """
    import logging

    log = logging.getLogger("security.rls")
    try:
        sync_info = _role_status(sync_engine)
        if sync_info is None:
            log.error("RLS status check: could not resolve current_user (sync engine)")
            return {"role": None, "bypassrls": None, "is_superuser": None}

        # Collect every role that actually serves traffic. The sync engine
        # (DATABASE_URL_SYNC: Celery workers + Alembic) is always checked. When
        # enforcing, the ASYNC engine (DATABASE_URL: FastAPI request traffic)
        # MUST be verified too — otherwise a non-bypassing sync role lets this
        # guard pass while the async API still has BYPASSRLS for every request,
        # if the two URLs point at different roles. (Codex review.)
        checked = {"sync": sync_info}
        if settings.RLS_ENFORCE:
            try:
                async_as_sync_url = settings.DATABASE_URL.replace(
                    "+asyncpg", ""
                ).replace(":6543/", ":5432/")
                tmp_engine = create_engine(
                    async_as_sync_url,
                    poolclass=NullPool,
                    connect_args={"connect_timeout": 10},
                )
                try:
                    async_info = _role_status(tmp_engine)
                finally:
                    tmp_engine.dispose()
            except Exception as exc:  # noqa: BLE001
                # Cannot verify the async role while enforcing → fail closed.
                raise RuntimeError(
                    f"RLS_ENFORCE: could not verify the async DB role: {exc}"
                ) from exc
            if async_info is None:
                raise RuntimeError(
                    "RLS_ENFORCE: could not resolve current_user on the async engine"
                )
            checked["async"] = async_info

        offenders = {
            label: info
            for label, info in checked.items()
            if info["bypassrls"] or info["is_superuser"]
        }
        if offenders:
            # REDTEAM HIGH T2: a BYPASSRLS/superuser runtime role makes the 025
            # WITH CHECK write policies (and all RLS) inert, leaving the tenant
            # boundary to the application WHERE filters alone.
            #
            # Enforcement is GATED behind RLS_ENFORCE (default False = advisory
            # log, today's behavior). We can only HARD-FAIL once the full RLS
            # cutover is in place — and it is NOT yet: the current prod role HAS
            # BYPASSRLS and worker paths depend on it (rls_sync_session() sets
            # app.current_user_id via SET LOCAL, cleared on the mid-task commit
            # in run_scrape_job; system_sync_session() sets no GUC for legitimate
            # cross-tenant ingest). Flip RLS_ENFORCE on only after the staged
            # cutover (role downgrade + per-transaction GUC reapply + a system
            # RLS policy) — the deferred HIGH-2 work on branch security/high-2-rls.
            detail = ", ".join(
                f"{label}={info['role']}(bypassrls={info['bypassrls']},"
                f"super={info['is_superuser']})"
                for label, info in offenders.items()
            )
            if settings.RLS_ENFORCE:
                log.error(
                    "RLS fail-closed: role(s) bypass RLS while RLS_ENFORCE=on — "
                    "refusing to start: %s", detail,
                )
                raise RuntimeError(
                    "Refusing to start: DB role(s) bypass RLS while RLS_ENFORCE "
                    f"is on ({detail}). Connect with non-BYPASSRLS roles."
                )
            log.error(
                "RLS advisory: role(s) bypass RLS — policies NOT enforced; tenant "
                "isolation relies on application WHERE filters only: %s", detail,
            )
        else:
            log.info("RLS advisory: DB role(s) do not bypass RLS — policies active.")
        return sync_info
    except RuntimeError:
        # REDTEAM HIGH T2: the production fail-closed above must
        # propagate, not be swallowed by the defensive handler below.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("RLS status check failed: %s", exc)
        return {"role": None, "bypassrls": None, "is_superuser": None}
