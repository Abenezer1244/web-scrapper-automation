"""Celery beat scheduler: 6 periodic tasks."""

from datetime import UTC, datetime, timedelta

from celery.schedules import crontab

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.scheduler")

# ─── Beat schedule ────────────────────────────────────────────────────────────

app.conf.beat_schedule = {
    "dispatch-scheduled-jobs": {
        "task": "src.workers.scheduler.dispatch_scheduled_jobs",
        "schedule": 60.0,  # every 1 minute
    },
    "watchdog-stuck-jobs": {
        "task": "src.workers.scheduler.watchdog_stuck_jobs",
        "schedule": 300.0,  # every 5 minutes
    },
    "canary-check": {
        "task": "src.workers.scheduler.canary_check",
        "schedule": 3600.0,  # every 1 hour
    },
    "reset-monthly-usage": {
        "task": "src.workers.scheduler.reset_monthly_usage",
        "schedule": crontab(hour=0, minute=0, day_of_month=1),  # 1st of month, midnight UTC
    },
    "scrape-county-daily": {
        "task": "src.workers.scheduler.scrape_county_daily",
        "schedule": crontab(hour=2, minute=0),
    },
    "purge-old-records": {
        "task": "src.workers.scheduler.purge_old_records",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),
    },
}


# ─── Task 1: Dispatch scheduled jobs ─────────────────────────────────────────

@app.task(name="src.workers.scheduler.dispatch_scheduled_jobs")
def dispatch_scheduled_jobs() -> None:
    """Enqueue jobs for all active scraper configs whose schedule matches now.

    Runs every minute. Idempotent — checks for an existing pending/running job
    for the same config before enqueuing to prevent duplicates.
    """
    import uuid

    from sqlalchemy import select

    from src.db.models import Job, ScraperConfig
    from src.db.session import SyncSessionLocal
    from src.workers.tasks import run_scrape_job

    now = datetime.now(UTC)
    enqueued = 0

    with SyncSessionLocal() as db:
        configs = db.execute(
            select(ScraperConfig).where(ScraperConfig.active)
        ).scalars().all()

        for config in configs:
            schedule = config.schedule or {}
            frequency = schedule.get("frequency", "manual")

            if frequency == "manual":
                continue

            if not _should_run_now(frequency, schedule.get("time", "06:00"), now):
                continue

            # Idempotency: skip if a job is already pending or running for this config
            active_statuses = {"pending", "queued", "probing", "scraping", "enriching"}
            existing = db.execute(
                select(Job).where(
                    Job.scraper_config_id == config.id,
                    Job.status.in_(active_statuses),
                )
            ).scalar_one_or_none()

            if existing:
                _logger.debug("Skipping %s — job already active (%s)", config.name, existing.status)
                continue

            job = Job(
                id=str(uuid.uuid4()),
                user_id=config.user_id,
                scraper_config_id=config.id,
                status="pending",
                trigger="scheduled",
            )
            db.add(job)
            db.flush()
            run_scrape_job.delay(job.id)
            enqueued += 1
            _logger.info("Scheduled job enqueued: %s (job_id=%s)", config.name, job.id)

        db.commit()

    if enqueued:
        _logger.info("dispatch_scheduled_jobs: enqueued %d jobs", enqueued)


def _should_run_now(frequency: str, run_time_str: str, now: datetime) -> bool:
    """Return True if this frequency + run_time combination should fire at `now`."""
    try:
        hour, minute = (int(x) for x in run_time_str.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 6, 0

    if now.hour != hour or now.minute != minute:
        return False

    if frequency == "daily":
        return True
    if frequency == "weekly":
        return now.weekday() == 0  # Monday
    if frequency == "monthly":
        return now.day == 1
    return False


# ─── Task 2: Watchdog for stuck jobs ─────────────────────────────────────────

@app.task(name="src.workers.scheduler.watchdog_stuck_jobs")
def watchdog_stuck_jobs() -> None:
    """Fail jobs that have been stuck in an active state for > 55 minutes.

    Runs every 5 minutes. Re-queues the job for retry up to max_retries times.
    EagleWeb chunked scraping can take 15-20min scrape + 15min DB save = 35min.
    """
    from sqlalchemy import select

    from src.db.models import Job
    from src.db.session import SyncSessionLocal
    from src.workers.tasks import run_scrape_job

    stuck_cutoff = datetime.now(UTC) - timedelta(minutes=55)
    active_statuses = {"queued", "probing", "scraping", "enriching"}

    with SyncSessionLocal() as db:
        stuck_jobs = db.execute(
            select(Job).where(
                Job.status.in_(active_statuses),
                Job.started_at < stuck_cutoff,
            )
        ).scalars().all()

        for job in stuck_jobs:
            if job.retry_count < 3:
                stuck_minutes = (
                    int((datetime.now(UTC) - job.started_at).total_seconds() / 60)
                    if job.started_at else "?"
                )
                job.retry_count += 1
                job.status = "pending"
                job.started_at = None
                db.flush()
                run_scrape_job.delay(job.id)
                _logger.warning(
                    "Watchdog: re-queued stuck job %s (attempt %d/3, stuck for %s min)",
                    job.id,
                    job.retry_count,
                    stuck_minutes,
                )
            else:
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
                job.error_message = (
                    "This scraper run did not complete in time. "
                    "Our team has been notified and will investigate."
                )
                _logger.error("Watchdog: permanently failed job %s after 3 retries", job.id)

        db.commit()


# ─── Task 3: Canary health checks ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.canary_check")
def canary_check() -> None:
    """Run a 1-page test scrape per active connector to verify portal health.

    Updates county_connectors.health_status:
      - 'healthy'  — canary returned ≥ 1 record
      - 'degraded' — canary returned 0 records (portal reachable but empty)
      - 'down'     — canary threw an exception
    """
    import asyncio

    from sqlalchemy import select

    from src.db.models import CountyConnector
    from src.db.session import SyncSessionLocal
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class

    with SyncSessionLocal() as db:
        all_connectors = db.execute(
            select(CountyConnector).where(CountyConnector.active)
        ).scalars().all()

        # Check max 5 counties per run (rotate through all over time)
        import random
        connectors = random.sample(all_connectors, min(5, len(all_connectors)))
        _logger.info("Canary: checking %d/%d counties", len(connectors), len(all_connectors))

        for connector in connectors:
            try:
                scraper_class = get_scraper_class(
                    connector.county, connector.state, connector.record_types[0]
                )
                # Probe a single day to minimise load on county portal
                today = datetime.now(UTC).date()
                yesterday = today - timedelta(days=1)

                records = asyncio.run(
                    _canary_scrape(scraper_class, yesterday.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y"))
                )
                connector.health_status = "healthy" if records else "degraded"
                _logger.info(
                    "Canary %s/%s: %s (%d records)",
                    connector.county, connector.state, connector.health_status, len(records)
                )
            except UnsupportedCountyError:
                _logger.warning("Canary: no scraper for %s/%s", connector.county, connector.state)
                connector.health_status = "down"
            except Exception as exc:
                _logger.error("Canary failed for %s/%s: %s", connector.county, connector.state, exc)
                connector.health_status = "down"

            connector.last_checked = datetime.now(UTC)

        db.commit()


async def _canary_scrape(scraper_class, date_from: str, date_to: str) -> list:
    async with scraper_class() as scraper:
        return await scraper.scrape(date_from, date_to)


# ─── Task 4: Monthly usage reset ─────────────────────────────────────────────

@app.task(name="src.workers.scheduler.reset_monthly_usage")
def reset_monthly_usage() -> None:
    """Reset records_used to 0 for all users on the 1st of each month.

    Runs at midnight UTC on the 1st. This clears the monthly quota so
    all plans get a fresh allocation each billing cycle.
    """
    from sqlalchemy import update

    from src.db.models import User
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as db:
        result = db.execute(update(User).values(records_used=0))
        db.commit()
        _logger.info("Monthly reset complete — cleared records_used for %d users", result.rowcount)


# ─── Task 5: Daily county scrape ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.scrape_county_daily")
def scrape_county_daily() -> None:
    """Dispatch daily scrape for each active county. Runs at 2 AM UTC."""
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


@app.task(name="src.workers.scheduler.run_single_county_scrape", queue="scrape")
def run_single_county_scrape(county: str, state: str) -> None:
    """Scrape a single county's daily records into county_records cache."""
    from src.workers.daily_scrape import run_daily_scrape_for_county

    try:
        count = run_daily_scrape_for_county(county, state)
        _logger.info("Daily scrape %s/%s: %d new records", county, state, count)
    except Exception:
        _logger.exception("Daily scrape failed for %s/%s", county, state)


# ─── Task 6: Purge old records ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.purge_old_records")
def purge_old_records() -> None:
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
        db.commit()
        _logger.info("Purged %d records older than %d days", result.rowcount, settings.RECORD_RETENTION_DAYS)
