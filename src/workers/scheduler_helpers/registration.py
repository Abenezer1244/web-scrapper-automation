"""Body logic for the registration beat task: purge expired pending signups."""

from datetime import UTC, datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _purge_expired_pending_registrations_impl() -> None:
    """Delete expired pending_registrations rows (email-verification flow).

    Each /auth/register attempt under EMAIL_VERIFICATION_ENABLED inserts its own
    pending row, and verify only drops the row(s) for a verified address. Signups
    that are never confirmed (abandoned, or sprayed by an attacker across IPs)
    otherwise linger — the expires_at filter stops them being honored but does not
    remove them. This hourly sweep deletes expired rows so the table cannot grow
    unbounded (Codex P2). Uses the cross-tenant system session: pending_registrations
    is pre-account and not tenant-scoped (no user_id), and the expires_at index
    keeps the delete cheap.
    """
    from sqlalchemy import delete

    from src.db.models import PendingRegistration
    from src.db.session import system_sync_session

    now = datetime.now(UTC)
    with system_sync_session() as db:
        result = db.execute(
            delete(PendingRegistration).where(PendingRegistration.expires_at < now)
        )
        db.commit()
        if result.rowcount:
            _logger.info("Purged %d expired pending registrations", result.rowcount)
