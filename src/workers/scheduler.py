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
        "schedule": crontab(hour=9, minute=0),  # 9 AM UTC = 2 AM PT (Seattle)
    },
    "purge-old-records": {
        "task": "src.workers.scheduler.purge_old_records",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),
    },
    "expire-trials": {
        "task": "src.workers.scheduler.expire_trials",
        "schedule": 3600.0,  # every 1 hour
    },
    "onboarding-emails": {
        "task": "src.workers.scheduler.send_onboarding_emails",
        "schedule": crontab(hour=14, minute=0),  # 2 PM UTC = 7 AM PT
    },
    "dispatch-pending-skip-trace": {
        # Sprint 4: drains pending_skip_trace_rows, submits Tracerfy batches.
        # Tracerfy rate-limits batch POSTs to 10 per 5 min, so we run every
        # 5 min and submit at most SKIP_TRACE_MAX_BATCHES_PER_TICK (default 2)
        # per tick. The task is a no-op if SKIP_TRACE_ENABLED=False.
        "task": "src.workers.skip_trace_dispatcher.dispatch_pending_skip_trace",
        "schedule": 300.0,  # every 5 minutes
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

    from src.db.models import Job, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.workers.tasks import run_scrape_job

    now = datetime.now(UTC)
    enqueued = 0
    skipped_limit = 0

    with SyncSessionLocal() as db:
        configs = db.execute(
            select(ScraperConfig).where(ScraperConfig.active)
        ).scalars().all()

        for config in configs:
            schedule = config.schedule or {}
            frequency = schedule.get("frequency", "manual")

            if frequency == "manual":
                continue

            # Build run time from run_at_hour/run_at_minute (frontend format)
            run_hour = schedule.get("run_at_hour", 6)
            run_minute = schedule.get("run_at_minute", 0)
            run_time_str = f"{run_hour}:{run_minute:02d}"

            if not _should_run_now(frequency, run_time_str, now):
                continue

            # Check user's record limit BEFORE creating the job
            user = db.execute(select(User).where(User.id == config.user_id)).scalar_one_or_none()
            if user and user.records_limit != -1 and user.records_used >= user.records_limit:
                _logger.info(
                    "Skipping %s — user %s at record limit (%d/%d)",
                    config.name, user.email, user.records_used, user.records_limit,
                )
                skipped_limit += 1
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

    if enqueued or skipped_limit:
        _logger.info(
            "dispatch_scheduled_jobs: enqueued %d, skipped %d (over limit)",
            enqueued, skipped_limit,
        )


def _should_run_now(frequency: str, run_time_str: str, now: datetime) -> bool:
    """Return True if this frequency + run_time combination should fire at `now`.

    Uses a ±1 minute tolerance window so that beat-tick drift (e.g. firing at
    06:01 instead of 06:00) does not skip an entire day's scheduled jobs.
    """
    try:
        hour, minute = (int(x) for x in run_time_str.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 6, 0

    # Check if we're within ±1 minute of the target time
    target_minutes = hour * 60 + minute
    current_minutes = now.hour * 60 + now.minute
    if abs(current_minutes - target_minutes) > 1:
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

    stuck_cutoff = datetime.now(UTC) - timedelta(minutes=20)
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
                scraper_class, _ = get_scraper_class(
                    connector.county, connector.state, connector.record_types[0]
                )
                # Probe a 7-day window. A 1-day window produces
                # false-positive "degraded" statuses for rural counties
                # that routinely file 0 probates or pre-foreclosures on
                # a given day. 7 days is short enough to stay cheap but
                # long enough that any county with meaningful filing
                # volume will return at least one record when healthy.
                today = datetime.now(UTC).date()
                week_ago = today - timedelta(days=7)

                records = asyncio.run(
                    _canary_scrape(scraper_class, week_ago.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y"))
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

    from datetime import UTC, datetime

    with SyncSessionLocal() as db:
        # Reset records_used and Sprint 4 skip_trace_used_this_month
        result = db.execute(
            update(User).values(
                records_used=0,
                skip_trace_used_this_month=0,
                skip_trace_period_start=datetime.now(UTC),
            )
        )
        db.commit()
        _logger.info(
            "Monthly reset complete — cleared records_used + skip_trace_used_this_month for %d users",
            result.rowcount,
        )


# ─── Task 5: Expire free trials ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.expire_trials")
def expire_trials() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users where trial_ends_at < now and plan is still 'pro'
    with no stripe_customer_id (paying users keep their plan).
    """
    from datetime import UTC, datetime

    from sqlalchemy import select, update

    from src.db.models import User
    from src.db.session import SyncSessionLocal

    now = datetime.now(UTC)

    with SyncSessionLocal() as db:
        # Find trial users whose trial has expired and who haven't paid
        expired = db.execute(
            select(User).where(
                User.trial_ends_at.isnot(None),
                User.trial_ends_at < now,
                User.plan != "starter",
                User.stripe_customer_id.is_(None),  # Not a paying customer
            )
        ).scalars().all()

        for user in expired:
            user.plan = "starter"
            user.records_limit = 50  # Starter limit
            _logger.info("Trial expired for %s — downgraded to starter", user.email)

        if expired:
            db.commit()
            _logger.info("Expired %d trials", len(expired))
        else:
            _logger.info("No expired trials to process")


# ─── Task 6: Daily county scrape ────────────────────────────────────────────

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


# ─── Task: Onboarding emails (daily at 7 AM PT) ─────────────────────────────

@app.task(name="src.workers.scheduler.send_onboarding_emails")
def send_onboarding_emails() -> None:
    """Send day-1 nudge, day-3 activation reminder, day 6-7 trial expiry warnings."""
    from sqlalchemy import select

    from src.db.models import Job, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.workers.onboarding_emails import (
        send_activation_reminder,
        send_day1_nudge,
        send_trial_ending_email,
    )

    now = datetime.now(UTC)
    day1_sent = 0
    day3_sent = 0
    expiry_sent = 0

    with SyncSessionLocal() as db:
        users = db.execute(
            select(User).where(User.is_active, User.trial_ends_at.isnot(None))
        ).scalars().all()

        for user in users:
            if not user.trial_ends_at:
                continue

            trial_end = user.trial_ends_at.replace(tzinfo=None) if user.trial_ends_at.tzinfo else user.trial_ends_at
            now_naive = now.replace(tzinfo=None)
            days_since_signup = (now_naive - user.created_at.replace(tzinfo=None)).days
            days_left = (trial_end - now_naive).days

            # Sprint 5.3 Day 1: nudge if the user still hasn't created
            # a scraper 24 hours after signup. Only runs once (this beat
            # task runs daily so days_since_signup==1 matches a ~24h window).
            if days_since_signup == 1:
                has_scraper = db.execute(
                    select(ScraperConfig).where(ScraperConfig.user_id == user.id)
                ).scalar_one_or_none() is not None
                if not has_scraper:
                    send_day1_nudge(user.email)
                    day1_sent += 1

            # Day 3: activation nudge (scraper exists but no downloads yet)
            if days_since_signup == 3:
                has_scraper = db.execute(
                    select(ScraperConfig).where(ScraperConfig.user_id == user.id)
                ).scalar_one_or_none() is not None

                has_download = db.execute(
                    select(Job).where(Job.user_id == user.id, Job.export_key.isnot(None))
                ).scalar_one_or_none() is not None

                send_activation_reminder(user.email, has_scraper, has_download)
                day3_sent += 1

            # Day 6 or 7: trial expiry warning
            if days_left in (1, 2):
                send_trial_ending_email(user.email, days_left)
                expiry_sent += 1

    _logger.info(
        "Onboarding email check: %d trial users evaluated (day1=%d day3=%d expiry=%d)",
        len(users), day1_sent, day3_sent, expiry_sent,
    )
