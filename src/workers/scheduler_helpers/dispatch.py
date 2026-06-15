"""Body logic for the schedule-dispatch beat tasks.

Holds the verbatim bodies of dispatch_scheduled_jobs and
dispatch_scheduled_batches, plus the shared _should_run_now / _dispatch_due_batches
helpers. scheduler.py re-exports _should_run_now and _dispatch_due_batches so
existing imports (and tests) keep resolving from src.workers.scheduler.
"""

from datetime import UTC, datetime

from src.config.constants import ACTIVE_STATUSES
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _dispatch_scheduled_jobs_impl() -> None:
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


def _dispatch_due_batches(db, now: datetime) -> list[str]:
    """Create a 'pending' BatchRun for every active, due, scheduled batch.

    Returns the created run ids (caller enqueues AFTER commit). Idempotency is
    DURABLE, not read-then-insert (Codex P1s):
      - occurrence key = the batch's scheduled TARGET minute on `now`'s date —
        NOT the tick minute. _should_run_now has a ±1-minute window, so two
        adjacent ticks both match one occurrence; keying on the tick minute
        would create two runs. uq_batch_runs_occurrence dedupes the second.
      - a still-active previous run also rejects the insert (partial unique
        uq_batch_runs_one_active) — a batch can't stack runs.
    Both collisions surface through INSERT .. ON CONFLICT DO NOTHING (no
    IntegrityError to race on; the loser is simply a no-op).
    """
    import uuid as _uuid

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import BatchRun, ScraperBatch, User

    created: list[str] = []
    batches = db.execute(
        select(ScraperBatch).where(ScraperBatch.status == "active")
    ).scalars().all()
    for batch in batches:
        schedule = batch.schedule or {}
        frequency = schedule.get("frequency", "manual")
        if frequency == "manual":
            continue
        run_hour = schedule.get("run_at_hour", 6)
        run_minute = schedule.get("run_at_minute", 0)
        if not _should_run_now(frequency, f"{run_hour}:{run_minute:02d}", now):
            continue

        # Quota gate at fire time (same boundary as dispatch_scheduled_jobs);
        # dispatch_batch_run re-checks at materialize time.
        user = db.get(User, batch.user_id)
        if user and user.records_limit != -1 and user.records_used >= user.records_limit:
            _logger.info(
                "dispatch_scheduled_batches: skipping batch %s — user at record limit",
                batch.id,
            )
            continue

        # NOTE (Codex, documented-not-fixed): _should_run_now's ±1-minute window
        # is not midnight-wraparound-aware, so a 23:59 target matches ticks
        # 23:58/23:59 (not next-day 00:00) and a 00:00 target matches
        # 00:00/00:01 (not prior-day 23:59). The matching ticks are therefore
        # always SAME-DAY as the target — this key is consistent for every tick
        # that can reach it, and no occurrence is ever missed (2 ticks still
        # match) or doubled. Fixing wraparound lives with the shared helper.
        occurrence = now.replace(
            hour=int(run_hour), minute=int(run_minute), second=0, microsecond=0
        )
        run_id = str(_uuid.uuid4())
        inserted = db.execute(
            pg_insert(BatchRun.__table__)
            .values(
                id=run_id,
                batch_id=batch.id,
                user_id=batch.user_id,
                status="pending",
                child_job_ids=[],
                excluded_no_date_count=0,
                dispatch_attempts=0,
                scheduled_for=occurrence,
            )
            .on_conflict_do_nothing()  # any unique: dup occurrence OR active run
        ).rowcount
        if inserted:
            created.append(run_id)
            _logger.info(
                "dispatch_scheduled_batches: run %s created for batch %s (%s)",
                run_id, batch.id, occurrence.isoformat(),
            )
    return created


def _dispatch_scheduled_batches_impl() -> None:
    """2B: enqueue a run for every active batch whose schedule matches now.

    Runs every minute (mirrors dispatch_scheduled_jobs). The created 'pending'
    run is the durable intent — if the .delay below is lost, batch_recovery_sweep
    re-dispatches it; everything downstream (fan-out, completion barrier,
    combined CSV, delivery) is the existing Track A machinery.
    """
    from src.db.session import system_sync_session
    from src.workers.batch_tasks import dispatch_batch_run

    now = datetime.now(UTC)
    with system_sync_session() as db:
        created = _dispatch_due_batches(db, now)
        db.commit()

    # Enqueue AFTER commit (commit-before-delay, like every other dispatcher).
    # Per-item try/except: a broker failure leaves the durable pending run for
    # the recovery sweep.
    for rid in created:
        try:
            dispatch_batch_run.delay(rid)
        except Exception as exc:  # noqa: BLE001 — recovered by the sweep
            _logger.warning(
                "dispatch_scheduled_batches: enqueue of %s failed (sweep recovers): %s",
                rid, str(exc)[:200],
            )
    if created:
        _logger.info("dispatch_scheduled_batches: %d run(s) created", len(created))
