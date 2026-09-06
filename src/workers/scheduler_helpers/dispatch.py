"""Body logic for the schedule-dispatch beat tasks.

Holds the verbatim bodies of dispatch_scheduled_jobs and
dispatch_scheduled_batches, plus the shared _should_run_now / _dispatch_due_batches
helpers. scheduler.py re-exports _should_run_now and _dispatch_due_batches so
existing imports (and tests) keep resolving from src.workers.scheduler.
"""

import calendar
from datetime import UTC, datetime, timedelta

from src.config.constants import ACTIVE_STATUSES
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Dedup window for scheduled single-config jobs. _should_run_now's ±1-minute
# tolerance fires up to 3 adjacent beat ticks (target-1, target, target+1) ~60s
# apart for one occurrence. The active-job check alone misses a duplicate when a
# fast scrape finishes (-> terminal, no longer ACTIVE) before the next tick, so
# we also block on a scheduled job CREATED within this window. 3 min covers the
# ~2-min tick span with margin; under an unchanged schedule, occurrences of one
# config are >=24h apart (daily/weekly/monthly) so it can never bridge two legit
# occurrences (a run-time edit within the window may suppress one fire). This is a
# single-beat mitigation, NOT a concurrency guarantee — the durable fix is a
# (config, occurrence) unique key like batches' uq_batch_runs_occurrence.
_SCHEDULED_DEDUP_MINUTES = 3


def _scheduled_dispatch_blocker_exists(db, config_id: str, now: datetime) -> bool:
    """True if dispatching a scheduled job for this config now would duplicate.

    Skips when EITHER (a) any job is currently active for the config (any
    trigger — the original overlap guard, so a scheduled run never starts on top
    of a manual/test run), OR (b) a `scheduled` job for this config was created
    within _SCHEDULED_DEDUP_MINUTES (catches the just-finished fast scrape so the
    target / target+1 ticks no-op). The trigger filter keeps a manual "Run now"
    from suppressing the scheduled occurrence and vice-versa.
    """
    from sqlalchemy import and_, or_, select

    from src.db.models import Job

    cutoff = now - timedelta(minutes=_SCHEDULED_DEDUP_MINUTES)
    return db.execute(
        select(Job.id)
        .where(
            Job.scraper_config_id == config_id,
            or_(
                Job.status.in_(ACTIVE_STATUSES),
                and_(Job.trigger == "scheduled", Job.created_at >= cutoff),
            ),
        )
        .limit(1)
    ).scalar() is not None


def _coerce_schedule_int(value: object, lo: int, hi: int, fallback: int) -> int:
    """Coerce a persisted-JSON schedule value to an int clamped to [lo, hi].

    Schedule values are range-validated by ScheduleConfig at the API boundary,
    but the stored JSON column can also hold legacy / hand-edited junk. Coercing
    here keeps a bad value from crashing the beat. Used by BOTH the run-time
    matcher and the batch occurrence key, so a value that passes the matcher can
    never then blow up `now.replace(hour=...)` (Codex P1).
    """
    try:
        return min(max(int(value), lo), hi)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _dispatch_due_jobs(db, now: datetime) -> list[str]:
    """Create a 'pending' Job for each active config whose schedule matches now.

    Returns the created job ids; the caller enqueues AFTER commit
    (commit-before-delay, mirroring _dispatch_due_batches). Skips configs that are
    manual, not due, over the user's record limit, or already have an active /
    recently-dispatched job (_scheduled_dispatch_blocker_exists). No broker I/O,
    so it is unit-testable with a fixed `now`.
    """
    import uuid
    from typing import cast

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.api.schemas import ScheduleConfigDict
    from src.db.models import Job, ScraperConfig, User

    created: list[str] = []
    skipped_limit = 0
    configs = db.execute(
        select(ScraperConfig).where(ScraperConfig.active)
    ).scalars().all()
    for config in configs:
        schedule: ScheduleConfigDict = cast(ScheduleConfigDict, config.schedule or {})
        frequency = schedule.get("frequency", "manual")

        if frequency == "manual":
            continue

        # Day/time selectors. run_at_weekday/run_at_day_of_month default to
        # Monday / the 1st so configs saved before the day picker existed keep
        # their old behavior (see ScheduleConfig contract). run_hour/run_minute
        # are captured here because they also form the occurrence key below.
        run_hour = schedule.get("run_at_hour", 6)
        run_minute = schedule.get("run_at_minute", 0)
        if not _should_run_now(
            frequency,
            now,
            run_hour,
            run_minute,
            schedule.get("run_at_weekday", 0),
            schedule.get("run_at_day_of_month", 1),
        ):
            continue

        # Record-limit gate BEFORE creating the job. WINDOW-AWARE: a raw
        # records_used read would skip a scheduled job on the PREVIOUS
        # entitlement window's usage during the gap before the lazy rollover
        # catches up. Also refuses a run for an account frozen on a failed
        # payment, with the honest reason in the log.
        from src.api.quota import quota_block_reason

        user = db.execute(select(User).where(User.id == config.user_id)).scalar_one_or_none()
        _blocked = quota_block_reason(user) if user else None
        if _blocked:
            _logger.info(
                "Skipping %s — user %s blocked: %s",
                config.name, user.email, _blocked,
            )
            skipped_limit += 1
            continue

        # Execution-time entitlement guard (audit until ENTITLEMENT_ENFORCEMENT).
        # A config can outlive a downgrade; re-validate against the CURRENT plan.
        from src.api.entitlements import ConfigRow, config_run_violation, should_block_run
        _active = db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
            ).where(ScraperConfig.user_id == config.user_id, ScraperConfig.active)
        ).all()
        if should_block_run(
            config_run_violation(
                user.plan if user else "starter", config.state, config.county,
                config.record_type, [ConfigRow(*r) for r in _active],
            ),
            user_id=str(config.user_id), plan=(user.plan if user else "starter"),
            context="schedule_single",
        ):
            continue

        # Idempotency, two layers:
        #  1. Cheap pre-check (_scheduled_dispatch_blocker_exists): skip if a job
        #     is already active for this config (overlap guard, any trigger) OR a
        #     scheduled job fired within the dedup window — avoids a wasted insert
        #     on every duplicate tick and stops a scheduled run starting on top of
        #     a manual one. This is read-then-check, so it can lose a race.
        #  2. Durable backstop: the unique (scraper_config_id, scheduled_for) so
        #     even two CONCURRENT beats that both pass the pre-check can't both
        #     create a job for the same occurrence — the second insert no-ops at
        #     the DB (Codex P1). Mirrors the batch path's uq_batch_runs_occurrence.
        if _scheduled_dispatch_blocker_exists(db, config.id, now):
            _logger.debug("Skipping %s — recent/active job for config", config.name)
            continue

        # Occurrence key = now truncated to the (coerced) run minute. Coerce with
        # the SAME helper the matcher used so a corrupted persisted hour/minute
        # that slipped past _should_run_now can't crash now.replace(), and the key
        # stays consistent with what actually fired.
        occurrence = now.replace(
            hour=_coerce_schedule_int(run_hour, 0, 23, 6),
            minute=_coerce_schedule_int(run_minute, 0, 59, 0),
            second=0,
            microsecond=0,
        )
        job_id = str(uuid.uuid4())
        # pg_insert (not ORM add/flush) so ON CONFLICT DO NOTHING is atomic at the
        # DB. It bypasses ORM Python-side default=, so every NOT-NULL column
        # without a server_default is set explicitly here (created_at and
        # billed_count have server_defaults; all other cols are nullable).
        inserted = db.execute(
            pg_insert(Job.__table__)
            .values(
                id=job_id,
                user_id=config.user_id,
                scraper_config_id=config.id,
                status="pending",
                trigger="scheduled",
                page_current=0,
                page_total=0,
                record_count=0,
                retry_count=0,
                scheduled_for=occurrence,
            )
            .on_conflict_do_nothing()  # dup (config, occurrence) -> no-op
        ).rowcount
        if inserted:
            created.append(job_id)
            _logger.info(
                "Scheduled job created: %s (job_id=%s, occurrence=%s)",
                config.name, job_id, occurrence.isoformat(),
            )
        else:
            _logger.debug(
                "Skipping %s — occurrence %s already dispatched (unique key)",
                config.name, occurrence.isoformat(),
            )

    if created or skipped_limit:
        _logger.info(
            "dispatch_scheduled_jobs: created %d, skipped %d (over limit)",
            len(created), skipped_limit,
        )
    return created


def _dispatch_scheduled_jobs_impl() -> None:
    """Enqueue jobs for all active scraper configs whose schedule matches now.

    Runs every minute. Idempotent. COMMIT-BEFORE-ENQUEUE (was enqueue-before-
    commit): the 'pending' rows commit FIRST, then the Celery tasks publish, so a
    worker can never consume a task before its row is durably visible to
    run_scrape_job's atomic pending->queued claim (the old order could strand a
    job 'pending' if the message was consumed pre-commit). A lost publish leaves a
    committed fresh-pending job that the watchdog re-delivers (health.py).
    """
    from src.db.session import SyncSessionLocal
    from src.workers.tasks import run_scrape_job

    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        created = _dispatch_due_jobs(db, now)
        db.commit()

    # Enqueue AFTER commit. Per-item try/except: a broker failure on one must not
    # abort the rest, and a lost publish is recovered by the watchdog.
    for jid in created:
        try:
            run_scrape_job.delay(jid)
        except Exception as exc:  # noqa: BLE001 — recovered by the watchdog
            _logger.warning(
                "dispatch_scheduled_jobs: enqueue of %s failed (watchdog recovers): %s",
                jid, str(exc)[:200],
            )


def _should_run_now(
    frequency: str,
    now: datetime,
    run_hour: int = 6,
    run_minute: int = 0,
    run_weekday: int = 0,
    run_day_of_month: int = 1,
) -> bool:
    """Return True if this schedule should fire at `now` (UTC).

    ±1-minute tolerance window so beat-tick drift (firing at 06:01 instead of
    06:00) does not skip an occurrence. `run_weekday` (0=Mon..6=Sun, matching
    datetime.weekday()) gates "weekly"; `run_day_of_month` (1..31, clamped to
    the month's last day so "31" fires on the last day of short months) gates
    "monthly". These come from a persisted JSON column, so they are coerced +
    range-clamped defensively here — a hand-edited / legacy bad value can't
    crash the beat or fire on a nonsense day. Defaults (Mon / 1st) reproduce
    the pre-picker hardcoded behavior for configs that lack the new keys.
    """
    run_hour = _coerce_schedule_int(run_hour, 0, 23, 6)
    run_minute = _coerce_schedule_int(run_minute, 0, 59, 0)
    run_weekday = _coerce_schedule_int(run_weekday, 0, 6, 0)
    run_day_of_month = _coerce_schedule_int(run_day_of_month, 1, 31, 1)

    # Check if we're within ±1 minute of the target time
    target_minutes = run_hour * 60 + run_minute
    current_minutes = now.hour * 60 + now.minute
    if abs(current_minutes - target_minutes) > 1:
        return False

    if frequency == "daily":
        return True
    if frequency == "weekly":
        return now.weekday() == run_weekday
    if frequency == "monthly":
        last_day = calendar.monthrange(now.year, now.month)[1]
        return now.day == min(run_day_of_month, last_day)
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
        # run_hour/run_minute are reused below for the occurrence key; weekday/
        # day-of-month default to Monday / the 1st for pre-picker batches.
        if not _should_run_now(
            frequency,
            now,
            run_hour,
            run_minute,
            schedule.get("run_at_weekday", 0),
            schedule.get("run_at_day_of_month", 1),
        ):
            continue

        from src.api.quota import quota_block_reason

        # Quota gate at fire time (same boundary as dispatch_scheduled_jobs);
        # dispatch_batch_run re-checks at materialize time. Covers both an
        # exhausted entitlement window and a payment freeze.
        user = db.get(User, batch.user_id)
        _blocked = quota_block_reason(user) if user else None
        if _blocked:
            _logger.info(
                "dispatch_scheduled_batches: skipping batch %s — %s",
                batch.id, _blocked,
            )
            continue

        # NOTE (Codex, documented-not-fixed): _should_run_now's ±1-minute window
        # is not midnight-wraparound-aware, so a 23:59 target matches ticks
        # 23:58/23:59 (not next-day 00:00) and a 00:00 target matches
        # 00:00/00:01 (not prior-day 23:59). The matching ticks are therefore
        # always SAME-DAY as the target, so this key is consistent for every tick
        # that can reach it and is never doubled. Under a healthy beat 2 same-day
        # ticks still match (only the cross-midnight tick is dropped); an
        # occurrence is missed only if the beat ALSO skips both same-day ticks.
        # Fixing true wraparound tolerance lives with the shared helper.
        # Coerce with the SAME helper the matcher used (Codex P1): a corrupted
        # persisted hour/minute that slips past _should_run_now must not then
        # crash now.replace(hour=...). Clamped here == clamped in the matcher,
        # so the occurrence key stays consistent with what fired.
        occurrence = now.replace(
            hour=_coerce_schedule_int(run_hour, 0, 23, 6),
            minute=_coerce_schedule_int(run_minute, 0, 59, 0),
            second=0,
            microsecond=0,
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
