"""Celery beat scheduler: 6 periodic tasks."""

from datetime import UTC, datetime, timedelta

from celery.schedules import crontab

from src.config.constants import ACTIVE_STATUSES, STUCK_CHECK_STATUSES
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
        # H5 (full-SaaS review): daily catch-up instead of cron-on-1st.
        # The task is idempotent — it only resets users whose
        # records_period_start is earlier than the current month, so
        # running it daily has the same net effect when Beat is
        # healthy and catches up cleanly when Beat was down at the
        # instant of the 1st-of-month rollover.
        "task": "src.workers.scheduler.reset_monthly_usage",
        "schedule": crontab(hour=0, minute=5),  # 00:05 UTC daily
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
    "flush-skip-trace-meter-outbox": {
        # REDTEAM (Codex convergence — meter outbox): recover skip-trace
        # MeterEvents whose inline post-commit enqueue was lost (broker down /
        # worker crash). Sweeps skip_trace_meter_events rows still
        # reported_at IS NULL and re-enqueues report_skip_trace_meter_event.
        "task": "src.workers.scheduler.flush_skip_trace_meter_outbox",
        "schedule": 180.0,  # every 3 minutes
    },
    "dialer-push-sweep": {
        # Phase 5: push dialer-ready leads for jobs whose async skip-trace has
        # SETTLED (can't push at scrape completion — cache-miss phones arrive
        # later via the Tracerfy webhook). Claims each job once via
        # Job.dialer_pushed_at. No-op when no config has a dialer_webhook_url.
        "task": "src.workers.scheduler.dialer_push_sweep",
        "schedule": 300.0,  # every 5 minutes
    },
    "refresh-public-sample-cache": {
        # RLS cutover Phase 2b: precompute the sanitized landing-page samples
        # so the public /scrapers/sample endpoint reads a cache row instead of
        # live-querying tenant tables (results/jobs/scraper_configs). Hourly is
        # plenty — the landing page tolerates stale-by-an-hour sample rows.
        "task": "src.workers.scheduler.refresh_public_sample_cache",
        "schedule": 3600.0,  # every 1 hour
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

        from typing import cast

        from src.api.schemas import ScheduleConfigDict
        for config in configs:
            schedule: ScheduleConfigDict = cast(ScheduleConfigDict, config.schedule or {})
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
            existing = db.execute(
                select(Job).where(
                    Job.scraper_config_id == config.id,
                    Job.status.in_(ACTIVE_STATUSES),
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
    from sqlalchemy import or_, select

    from src.db.models import Job
    from src.db.session import system_sync_session
    from src.workers.tasks import run_scrape_job

    now = datetime.now(UTC)
    stuck_cutoff = now - timedelta(minutes=20)
    # A job stuck in 'queued' state with started_at=NULL is a zombie
    # — the worker died before it could mark the job started. The
    # old predicate `Job.started_at < stuck_cutoff` returned NULL
    # for those rows and Postgres filtered them OUT, so they were
    # invisible to the watchdog forever. H8 from the full-SaaS
    # review: catch them via a separate "queued forever" branch.
    queued_cutoff = now - timedelta(minutes=10)

    with system_sync_session() as db:
        stuck_jobs = db.execute(
            select(Job).where(
                Job.status.in_(STUCK_CHECK_STATUSES),
                or_(
                    Job.started_at < stuck_cutoff,
                    # Zombie: never got a started_at, but was created
                    # more than 10 minutes ago. A legitimate job goes
                    # from pending → queued → probing within seconds.
                    (Job.started_at.is_(None))
                    & (Job.created_at < queued_cutoff),
                ),
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
                # M12 (full-SaaS review): also reset the progress
                # counters so the UI doesn't show nonsense like
                # "Page 3 of 5" after a job was re-queued from
                # page 3. The retried scrape starts over from page 1.
                job.page_current = 0
                job.page_total = 0
                job.record_count = 0
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

    from src.api.middleware.security import register_connector_domains_from_db
    from src.db.models import CountyConnector
    from src.db.session import SyncSessionLocal
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class

    # Refresh the in-process SSRF allowlist so connectors added via
    # POST /scrapers/connectors after this worker booted are not falsely
    # marked `down` by the next canary cycle. Without this, the API
    # process's add_scrape_domain() call doesn't propagate to the worker
    # that runs canary_check, the canary's validate_scraping_target()
    # rejects the new base_url, and list_connectors() then hides the
    # connector from users (it filters out `down` rows by default).
    register_connector_domains_from_db()

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
                # Sticky health: once a connector has been marked
                # 'healthy', do NOT downgrade it to 'degraded' just
                # because the most recent 7-day probe returned zero
                # records. Small counties oscillate based on which
                # week the canary happens to sample, and flipping the
                # status causes them to vanish from the user-facing
                # connectors endpoint. Only a real exception path
                # (caught below) downgrades a healthy connector.
                # Non-healthy connectors still get upgraded normally
                # when they produce records.
                if records:
                    connector.health_status = "healthy"
                elif connector.health_status != "healthy":
                    # Was degraded/down/unknown and is still empty —
                    # stay in whatever non-healthy state we had, or
                    # move to 'degraded' if we were 'unknown'.
                    if connector.health_status in ("unknown", "down"):
                        connector.health_status = "degraded"
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


# ─── Task 4: Monthly usage reset (daily catch-up) ────────────────────────────

@app.task(name="src.workers.scheduler.reset_monthly_usage")
def reset_monthly_usage() -> None:
    """Roll over monthly usage counters when the billing period ends.

    H5 (full-SaaS review): previously this ran on a crontab at
    day_of_month=1, hour=0, minute=0. Celery Beat does NOT backfill
    missed cron ticks — if Beat was down at that instant (Railway
    redeploy, broker hiccup) the reset was SKIPPED ENTIRELY and every
    user carried last month's records_used forward into the new
    month. A user at 500/500 last month started the new month
    instantly at cap.

    Now runs daily at 00:05 UTC. On each run, finds users whose
    records_period_start points at a month strictly earlier than
    this run's current month and resets their counters +
    advances records_period_start to the first of the current
    month. Idempotent: a user who was already reset this month
    has records_period_start = this month and is skipped.

    The same logic applies to skip_trace_used_this_month +
    skip_trace_period_start for Sprint 4 billing.
    """
    from datetime import UTC, datetime

    from sqlalchemy import text

    from src.db.session import system_sync_session

    now = datetime.now(UTC)

    with system_sync_session() as db:
        # Truncate to first-of-current-month at UTC. Postgres
        # date_trunc('month', ...) gives us the boundary.
        result = db.execute(
            text("""
                WITH period_start AS (
                    SELECT date_trunc('month', NOW() AT TIME ZONE 'UTC')
                           AT TIME ZONE 'UTC' AS this_month
                )
                UPDATE users
                SET
                    records_used = 0,
                    records_period_start = (SELECT this_month FROM period_start),
                    skip_trace_used_this_month = 0,
                    skip_trace_period_start = (SELECT this_month FROM period_start)
                WHERE
                    records_period_start IS NULL
                    OR records_period_start
                       < (SELECT this_month FROM period_start)
            """)
        )
        db.commit()
        _logger.info(
            "Daily usage rollover: reset %d users whose period_start "
            "was earlier than %s",
            result.rowcount, now.isoformat(),
        )


# ─── Task 5: Expire free trials ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.expire_trials")
def expire_trials() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users where trial_ends_at < now and plan is still 'pro'
    with no stripe_customer_id (paying users keep their plan).
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

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
        mem = db.execute(
            text("DELETE FROM property_list_membership WHERE last_seen_at < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
        _logger.info(
            "Purged %d county_records and %d membership rows older than %d days",
            result.rowcount, mem.rowcount, settings.RECORD_RETENTION_DAYS,
        )


# ─── Task: Refresh public sample cache (landing page) ───────────────────────

@app.task(name="src.workers.scheduler.refresh_public_sample_cache")
def refresh_public_sample_cache() -> None:
    """Recompute the sanitized landing-page samples + stats into
    public_sample_cache (RLS cutover Phase 2b).

    The public /scrapers/sample endpoint reads ONLY this precomputed row, so an
    unauthenticated request never live-queries the tenant tables
    (results/jobs/scraper_configs) and the API role needs no cross-tenant read
    policy for it. ALL PII redaction happens HERE, so the cached payload is safe
    to serve publicly. Runs via system_sync_session (cross-tenant, no RLS user
    context) — under the cutover the bridgeleads_system FOR ALL policy applies.
    """
    import json

    from sqlalchemy import func as sa_func
    from sqlalchemy import select, text

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    def _generalize_address(addr: str | None) -> str | None:
        # Drop the leading street segment; keep city/state/ZIP. 1 part (no
        # comma) → can't isolate the street safely, so redact. (mirrors N2)
        if not addr:
            return None
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        return ", ".join(parts[1:]) if len(parts) >= 2 else None

    def _fmt_count(n: int) -> str:
        if n < 1000:
            return f"{n}+"
        if n < 10_000:
            return f"{round(n / 1000, 1)}K+"
        return f"{n // 1000:,}K+"

    with system_sync_session() as db:
        rows = db.execute(
            select(Result, ScraperConfig.county)
            .join(Job, Result.job_id == Job.id)
            .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
            .where(
                Job.status == "done",
                Result.property_address.isnot(None),
                Result.property_address != "",
                Result.mailing_address.isnot(None),
                Result.mailing_address != "",
            )
            .order_by(Job.created_at.desc())
            .limit(5)
        ).all()

        samples = []
        for r, county_slug in rows:
            # Partially anonymize: first name + last initial.
            name = r.party_name or ""
            parts = name.split()
            if len(parts) >= 2:
                anon_name = f"{parts[0]} {parts[1][0]}."
            else:
                anon_name = f"{parts[0][0]}." if parts else "—"
            samples.append({
                # date_recorded is String(32) in the model, not a date — store
                # it verbatim (matches the old inline /sample behavior exactly).
                "date_recorded": r.date_recorded,
                "party_name": anon_name,
                "county": (county_slug or "").title(),
                "property_address": _generalize_address(r.property_address),
                "mailing_address": _generalize_address(r.mailing_address),
                "has_parcel": bool(r.parcel_id),
            })

        delivered_count = db.execute(
            select(sa_func.count(Result.id)).where(
                Result.property_address.isnot(None),
                Result.property_address != "",
            )
        ).scalar() or 0

        counties_active = db.execute(
            select(sa_func.count(sa_func.distinct(ScraperConfig.county)))
            .select_from(Result)
            .join(Job, Result.job_id == Job.id)
            .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
            .where(
                Result.property_address.isnot(None),
                Result.property_address != "",
            )
        ).scalar() or 0

        payload = {
            "records": samples,
            "total_scraped": _fmt_count(delivered_count),
            "counties_active": counties_active,
            "enrichment_rate": "95%+",
            "freshness": "Updated daily",
        }

        # Singleton upsert (id is fixed at 1 by the table default + CHECK).
        db.execute(
            text("""
                INSERT INTO public.public_sample_cache (id, payload, refreshed_at)
                VALUES (1, CAST(:payload AS jsonb), now())
                ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload, refreshed_at = now()
            """),
            {"payload": json.dumps(payload)},
        )
        db.commit()
        _logger.info("Refreshed public_sample_cache: %d samples", len(samples))


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


# ─── Task: Flush skip-trace meter outbox (every 3 min) ──────────────────────

@app.task(name="src.workers.scheduler.flush_skip_trace_meter_outbox")
def flush_skip_trace_meter_outbox() -> None:
    """Recover skip-trace Stripe MeterEvents whose inline enqueue was lost.

    REDTEAM (Codex convergence — meter outbox): the Tracerfy ingest worker
    commits a skip_trace_meter_events outbox row per billable user in the same
    transaction that advances the usage counter, then best-effort enqueues
    report_skip_trace_meter_event for each. If the broker was down at that
    instant — or the worker crashed between commit and enqueue — the row sits
    with reported_at IS NULL and would never be billed. This sweep picks those
    up and re-enqueues them.

    Runs every ~3 minutes. Only sweeps rows older than 30 seconds so the inline
    enqueue gets first crack (avoids a duplicate enqueue racing the fast path;
    the report task is idempotent on reported_at anyway). The report task fires
    the Stripe MeterEvent with a stable (queue_id, user_id) identifier, so a
    re-enqueue can neither lose the event nor double-bill.
    """
    from sqlalchemy import text

    from src.db.session import system_sync_session
    from src.workers.tracerfy_ingest import report_skip_trace_meter_event

    with system_sync_session() as db:
        rows = db.execute(
            text("""
                SELECT id
                FROM skip_trace_meter_events
                WHERE reported_at IS NULL
                  AND created_at < NOW() - INTERVAL '30 seconds'
            """)
        ).fetchall()

    enqueued = 0
    for row in rows:
        try:
            report_skip_trace_meter_event.delay(str(row.id))
            enqueued += 1
        except Exception as exc:  # noqa: BLE001 — broker still down; try next sweep
            _logger.error(
                "Outbox sweep: failed to re-enqueue meter report for outbox "
                "%s: %s — will retry next sweep",
                row.id, str(exc)[:200],
            )

    if enqueued:
        _logger.info(
            "Skip-trace meter outbox sweep: re-enqueued %d unreported event(s)",
            enqueued,
        )


# ─── Task: Dialer push sweep (Phase 5) ───────────────────────────────────────

@app.task(name="src.workers.scheduler.dialer_push_sweep")
def dialer_push_sweep() -> None:
    """Push dialer-ready leads for done jobs whose skip-trace has SETTLED.

    Deferred from scrape completion on purpose: skip-trace is async — cache-miss
    rows are marked queued/submitted and their phone/DNC are filled in later by
    the Tracerfy webhook, so a push at completion would miss exactly the leads
    we want (Codex). A job is "settled" when no Result of it is still
    queued/submitted. Each job is claimed once via Job.dialer_pushed_at, so even
    a job with zero dialer-ready leads is evaluated only once. Reuses
    deliver_job_webhook (SSRF re-validate, HMAC, retry, non-fatal). No-op when no
    config has a dialer_webhook_url.
    """
    from sqlalchemy import and_, func, or_, select, update

    from src.api.dialer_filters import dialer_ready_conditions
    from src.config.constants import BUSINESS_FEATURES_PLANS
    from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, User
    from src.db.session import system_sync_session
    from src.workers.dialer_connectors import get_connector
    from src.workers.webhook_delivery import DIALER_PUSH_CAP, deliver_job_webhook

    _BATCH = 50
    # Jobs still waiting on async skip-trace. Base "settled" on the pending QUEUE
    # (the authoritative async-work tracker), NOT Result.skip_trace_status: when
    # Tracerfy submission errors, the dispatcher marks the pending row 'errored'
    # but leaves Result 'queued', so a Result-based check would NEVER settle that
    # job (Codex). queued/submitted = in-flight; completed/errored = terminal.
    #
    # Settlement nuance (Codex), queued vs submitted differ:
    #  - 'queued' rows are local dispatcher backlog — the dispatcher WILL process
    #    them even if it's down/backlogged for a while, so they ALWAYS block
    #    settlement until they leave the queue. Aging them out would claim the
    #    job before the phones exist, and it would never push again.
    #  - 'submitted' rows are in-flight at Tracerfy and CAN get stuck if the
    #    completed CSV omits them; those age out past the cutoff so a wedged row
    #    can't block the push forever. Age by submitted_at (COALESCE to
    #    enqueued_at only as a defensive fallback for a NULL submitted_at).
    _stale_cutoff = datetime.now(UTC) - timedelta(hours=12)
    _submitted_age = func.coalesce(
        PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.enqueued_at
    )
    unsettled = (
        select(PendingSkipTraceRow.id)
        .where(
            PendingSkipTraceRow.job_id == Job.id,
            or_(
                PendingSkipTraceRow.status == "queued",
                and_(
                    PendingSkipTraceRow.status == "submitted",
                    _submitted_age >= _stale_cutoff,
                ),
            ),
        )
        .exists()
    )

    with system_sync_session() as db:
        candidates = db.execute(
            select(Job, ScraperConfig)
            # Owner-match in the join (Codex security, defense-in-depth): the DB
            # doesn't enforce job.user_id == config.user_id, and this sweep runs
            # in a system session that bypasses RLS — without this, a malformed
            # job (user A) pointing at user B's config could push A's lead PII to
            # B's dialer_webhook_url. The job-create path already enforces it; this
            # closes the gap if a bad row ever exists.
            .join(
                ScraperConfig,
                (ScraperConfig.id == Job.scraper_config_id)
                & (ScraperConfig.user_id == Job.user_id),
            )
            # Re-check entitlement at push time against the owner's CURRENT plan:
            # the dialer push is a Business+ feature, and a user who configured it
            # then downgraded must NOT keep pushing lead PII (Codex). Gating in SQL
            # also keeps downgraded users' jobs out of the candidate set entirely.
            .join(User, User.id == Job.user_id)
            .where(
                Job.status == "done",
                Job.dialer_pushed_at.is_(None),
                User.plan.in_(BUSINESS_FEATURES_PLANS),
                # ->> returns NULL for missing key or non-text; works for JSON/JSONB.
                ScraperConfig.deliver.op("->>")("dialer_webhook_url").isnot(None),
                ScraperConfig.deliver.op("->>")("dialer_webhook_url") != "",
                ~unsettled,
            )
            .limit(_BATCH)
        ).all()

        pushed = 0
        for job, config in candidates:
            deliver = config.deliver or {}
            url = deliver.get("dialer_webhook_url")
            if not url:
                continue  # defensive: SQL filter should already guarantee this
            # DURABLE CLAIM BEFORE PUBLISH (at-most-once): atomically stamp
            # dialer_pushed_at from NULL and COMMIT before enqueuing. This both
            # serializes concurrent sweeps (only one UPDATE flips NULL->now) and
            # closes the publish-before-commit window — if we enqueued first and
            # then crashed pre-commit, the next sweep would re-push (Codex). A
            # lost push after a successful claim is recoverable via manual export;
            # a duplicate dialer import is not, so claim-first is the safe order.
            claimed = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.dialer_pushed_at.is_(None))
                .values(dialer_pushed_at=datetime.now(UTC))
            ).rowcount
            db.commit()
            if not claimed:
                continue  # another sweep claimed it first
            try:
                # NOT-known-DNC (include_unknown_dnc=True): skip-trace populates
                # phone but leaves phone_dnc_flag NULL (Tracerfy returns no DNC),
                # so a strict `IS FALSE` would match nothing and the feature would
                # push zero leads (Codex). The destination dialer performs the
                # authoritative DNC scrub; this excludes only KNOWN-DNC numbers
                # and is forward-safe if DNC data is added later.
                conds = dialer_ready_conditions(include_unknown_dnc=True)
                # Exclude cross-job duplicates: a recurring scrape re-finds an
                # already-delivered lead and stores it is_duplicate=true; pushing
                # it (with a NEW result id as external_id) would re-import the same
                # contact every run (Codex). Push only fresh leads.
                conds = [*conds, Result.is_duplicate.is_(False)]
                total = db.execute(
                    select(func.count()).select_from(Result).where(
                        Result.job_id == job.id, Result.user_id == job.user_id, *conds
                    )
                ).scalar_one()
                if total > 0:
                    rows = db.execute(
                        select(Result)
                        .where(Result.job_id == job.id, Result.user_id == job.user_id, *conds)
                        .order_by(Result.id)  # deterministic before LIMIT
                        .limit(DIALER_PUSH_CAP)
                    ).scalars().all()
                    leads = [
                        {
                            "id": row.id,
                            "party_name": row.party_name,
                            "phone": row.phone,
                            "phone_type": row.phone_type,
                            "phone_dnc_flag": row.phone_dnc_flag,
                            "email": row.email,
                            "property_address": row.property_address,
                            "mailing_address": row.mailing_address,
                        }
                        for row in rows
                    ]
                    # Dispatch via the dialer connector seam (Thread 3). For the
                    # generic webhook this builds the exact same payload + URL as
                    # before (locked by tests/test_dialer_connector_base.py) and
                    # enqueues one delivery — byte-identical to the prior path.
                    connector = get_connector(deliver.get("dialer_type"))
                    job_meta = {
                        "job_id": str(job.id),
                        "scraper_config_id": str(config.id),
                        "scraper_name": config.name,
                        "county": config.county,
                        "state": config.state,
                        "record_type": config.record_type,
                        "total_dialer_ready_count": total,
                        "dialer_webhook_url": url,
                        "dialer_webhook_secret": deliver.get("dialer_webhook_secret"),
                    }
                    for req in connector.build_requests(leads, job_meta):
                        deliver_job_webhook.delay(str(job.id), req["url"], req["payload"])
                    pushed += 1
                    _logger.info(
                        "Dialer push queued for job %s: %d of %d dialer-ready leads",
                        job.id, len(leads), total,
                    )
                # Already claimed+committed above. A 0-lead job stays claimed so
                # it isn't re-evaluated every sweep forever.
            except Exception as exc:  # noqa: BLE001 — one bad job must not stall the sweep
                # The enqueue is the LAST step in the try and only raises BEFORE
                # the message is sent, so reaching here means nothing was pushed.
                # Un-claim (dialer_pushed_at back to NULL) so a transient error
                # retries next sweep — without reopening the duplicate-push window
                # (a crash here leaves the claim set, which is the safe direction).
                # Never log the URL (token risk) or lead PII — message only.
                try:
                    db.rollback()
                    db.execute(
                        update(Job)
                        .where(Job.id == job.id)
                        .values(dialer_pushed_at=None)
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                _logger.warning(
                    "Dialer push sweep: job %s failed (will retry next sweep): %s",
                    job.id, str(exc)[:200],
                )

        if pushed:
            _logger.info("Dialer push sweep: enqueued %d job push(es)", pushed)
