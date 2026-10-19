"""Body logic for the county beat task purge_old_records."""

from datetime import UTC, datetime, timedelta

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _purge_old_records_impl() -> None:
    """Delete county_records older than RECORD_RETENTION_DAYS. Weekly."""
    from sqlalchemy import text

    from src.config import settings
    from src.db.session import SyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(days=settings.RECORD_RETENTION_DAYS)

    with SyncSessionLocal() as db:
        result = db.execute(
            text("DELETE FROM county_records WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        mem = db.execute(
            text("DELETE FROM property_list_membership WHERE last_seen_at < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
        _logger.info(
            "Purged %d county_records and %d membership rows older than %d days",
            result.rowcount, mem.rowcount, settings.RECORD_RETENTION_DAYS,
        )
