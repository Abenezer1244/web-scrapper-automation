"""Notification routes (Phase 2b): user-scoped read + mark-read.

System (worker) writes notifications; the API only reads + marks read. Every
query filters by current_user.id (defense-in-depth over RLS). The API never
uses system_sync_session."""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.schemas import (
    NotificationListResponse,
    NotificationResponse,
    ReadAllResponse,
)
from src.db.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> NotificationListResponse:
    rows = (
        await db.execute(
            select(Notification)
            .where(Notification.user_id == current_user.id)
            .order_by(Notification.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    unread = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == current_user.id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(r) for r in rows],
        unread_count=unread,
    )


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> NotificationResponse:
    try:
        uuid.UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    # Never db.get() before the user filter.
    row = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
    await db.flush()
    return NotificationResponse.model_validate(row)


@router.post("/read-all", response_model=ReadAllResponse)
async def mark_all_read(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ReadAllResponse:
    result = await db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    await db.flush()
    return ReadAllResponse(updated=result.rowcount or 0)
