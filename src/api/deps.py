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
    """
    await db.execute(
        text("SET LOCAL app.current_user_id = :uid"),
        {"uid": current_user.id},
    )
    return db
