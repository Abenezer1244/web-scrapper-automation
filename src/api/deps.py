"""Shared FastAPI dependencies.

Separated from auth.py and session.py to avoid circular imports.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import get_current_user
from src.db.models import User
from src.db.session import get_db


async def get_rls_db(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncSession:
    """Yields an async DB session with PostgreSQL RLS enforced for the current user.

    Sets `app.current_user_id` via SET LOCAL so all RLS policies on
    scraper_configs, jobs, results, and job_logs are active for this transaction.
    Defence-in-depth on top of the application-level WHERE user_id = ... filters.

    The user_id is stored on the underlying sync session's `.info` so the
    `after_begin` listener in src/db/session.py re-applies the GUC on every
    transaction — `set_config(local=true)` is transaction-scoped and would
    otherwise be lost if a handler commits mid-request (matters once the
    runtime role no longer bypasses RLS). The explicit set_config below covers
    the current transaction immediately.
    """
    # sync_session.info is the durable per-session store the after_begin
    # listener reads; AsyncSession proxies to it.
    db.sync_session.info["rls_user_id"] = str(current_user.id)
    # Use set_config() with parameterized binding to prevent SQL injection
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(current_user.id)},
    )
    return db
