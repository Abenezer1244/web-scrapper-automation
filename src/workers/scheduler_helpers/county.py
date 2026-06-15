"""Body logic for the county beat tasks: scrape_county_daily,
run_single_county_scrape, purge_old_records.

run_single_county_scrape stays a task in scheduler.py (it has queue="scrape"
and is .delay()-ed by scrape_county_daily). scrape_county_daily references it
via a callable passed in from scheduler.py so the registered task is the one
fanned out — the impl never imports a second copy.
"""

from datetime import UTC, datetime, timedelta

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _scrape_county_daily_impl(run_single_county_scrape) -> None:
    """Dispatch daily scrape for each active county. Runs at 2 AM UTC.

    `run_single_county_scrape` is the registered Celery task object, passed in
    from scheduler.py so the fan-out targets the real registered task.
    """
    from src.config import settings

    if not settings.ENABLE_DAILY_SCRAPE:
        return

    from sqlalchemy import select

    from src.db.models import CountyConnector
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as db:
        connectors = db.execute(
            select(CountyConnector).where(CountyConnector.active)
        ).scalars().all()

    _logger.info("Daily scrape: dispatching %d counties", len(connectors))

    for conn in connectors:
        run_single_county_scrape.delay(conn.county, conn.state)


def _run_single_county_scrape_impl(county: str, state: str) -> None:
    """Scrape a single county's daily records into county_records cache."""
    # Refresh the in-process SSRF allowlist from the connectors table before
    # scraping. Without this, a connector added through POST /scrapers/connectors
    # while this worker was already running would be rejected by
    # validate_scraping_target() because its host hasn't been registered in
    # this process's _ALLOWED_SCRAPE_DOMAINS frozenset (which is loaded only
    # at worker_ready). Idempotent and cheap (one query, set-union).
    from src.api.middleware.security import register_connector_domains_from_db
    from src.workers.daily_scrape import run_daily_scrape_for_county

    register_connector_domains_from_db()

    try:
        count = run_daily_scrape_for_county(county, state)
        _logger.info("Daily scrape %s/%s: %d new records", county, state, count)
    except Exception:
        _logger.exception("Daily scrape failed for %s/%s", county, state)


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
