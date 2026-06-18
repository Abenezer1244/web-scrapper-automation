"""System-side in-app notification emit (Phase 2b).

The ONLY notification write path. Opens its own system_sync_session (system
role, notifications_system FOR ALL) — callers never pass a db. Used by the
worker job done/failed transitions and the emit_payment_notification Celery
task. Best-effort: never raises into the caller; a notification is not worth
failing a job/webhook over. Gated by the user's notification_prefs[type] (one
toggle governs both email and in-app); fails closed on unknown types.
"""
from __future__ import annotations

from src.config.constants import NotificationType
from src.db.models import Notification, User
from src.db.session import system_sync_session
from src.utils.logger import setup_logger

_logger = setup_logger("workers.notification_emit")

_VALID_TYPES = {t.value for t in NotificationType}


def create_notification(
    *,
    user_id: str,
    type: str,
    job_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Emit a single in-app notification row, best-effort.

    Opens its own system_sync_session — callers must NOT pass a db handle.
    Silently skips the insert when:
      - ``type`` is not a recognised NotificationType value (fail-closed)
      - the user's notification_prefs[type] is explicitly False (pref-gated;
        absent key = enabled)
      - the user record cannot be found

    The broad ``except Exception`` below is intentional and the ONLY sanctioned
    broad-except in this codebase: notification emit is strictly best-effort and
    must never propagate into a Celery task or webhook handler.
    """
    try:
        if type not in _VALID_TYPES:
            _logger.warning("create_notification: unknown type %r — skipping", type)
            return
        with system_sync_session() as db:
            user = db.get(User, user_id)
            if user is None:
                _logger.warning("create_notification: user %s not found", user_id)
                return
            prefs = user.notification_prefs or {}
            # One toggle governs both email + in-app; absent key = enabled.
            if prefs.get(type, True) is False:
                return
            db.add(Notification(
                user_id=user_id, type=type, job_id=job_id, detail=detail,
            ))
            db.commit()
    except Exception as exc:  # best-effort: never propagate  # noqa: BLE001
        _logger.warning("create_notification failed (non-fatal): %s", str(exc)[:200])
