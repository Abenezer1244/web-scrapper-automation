"""Celery task: full scrape job lifecycle.

State machine:
    PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
                                                        → FAILED
"""

import asyncio
import json
import time
from datetime import datetime

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy import text as sa_text

from src.config.constants import (
    PRIORITY_QUEUE_PLANS,
    SCRAPE_TRANSIENT_BACKOFF_SECONDS,
    SCRAPE_TRANSIENT_MAX_RETRIES,
)
from src.scrapers.probate import (
    classify_probate_signal_for_row,
    should_include_probate_row,
)
from src.utils.address_intel import compute_owner_flags
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.property_identity import (
    compute_property_key as _compute_property_key,  # noqa: F401  (re-export)
)
from src.workers.property_identity import legacy_strong_signature as _legacy_strong_signature

# ─── Re-exports (preserve the historical src.workers.tasks import surface) ────
# The pipeline-phase helpers were relocated to src/workers/tasks_helpers/ to
# shrink this module. They are re-imported here so existing callers/tests that
# do `from src.workers.tasks import <name>` keep working unchanged, and so the
# run_scrape_job body below can reference them as before. No logic moved with
# them — the bodies are byte-identical to their former definitions here.
from src.workers.tasks_helpers.dates import (  # noqa: F401  (re-export)
    _resolve_date_range,
    _to_mmddyyyy,
)
from src.workers.tasks_helpers.dedup import (  # noqa: F401  (re-export)
    _TRUSTED_TAX_SOURCES,
    _extract_tax_fields,
    _upsert_property_membership,
    _write_result_property_keys,
    validate_tax_delinquent_records,
)
from src.workers.tasks_helpers.enrich import (  # noqa: F401  (re-export)
    _enqueue_skip_trace_rows,
    _reuse_enrichment_for_duplicates,
    _run_inline_enrichment,
    _run_scraper,
)
from src.workers.tasks_helpers.status import (
    _DELIVERY_TOKEN_TTL,  # noqa: F401  (re-export)
    _TERMINAL_STATUSES,
    HeartbeatThread,
    JobUpdateFields,  # noqa: F401  (re-export)
    _delivery_download_url,
    _fail_job,
    _now,
    _publish_log,
    _redis,
    _retry_scrape_job,
    _set_status,
)

_logger = setup_logger("worker.task")

# R2 export-upload retry policy. A failed upload means no deliverable (the local
# file is deleted and both delivery paths need the object key), so we retry a few
# times before failing the job rather than stranding a paying user.
_R2_UPLOAD_ATTEMPTS = 3
_R2_UPLOAD_BACKOFF = 2  # seconds, multiplied by attempt number (2s, 4s)


def _upload_export_with_retry(exporter, local_file, object_key) -> tuple[bool, Exception | None]:
    """Upload an export to R2 with bounded retries. Never raises.

    Returns ``(ok, last_exception)``: ``(True, None)`` on the first successful
    upload, else ``(False, <last error>)`` after exhausting the attempts. R2
    blips are usually transient, so a few spaced retries recover most of them
    before the caller has to fail the job. Pure (no DB / no file deletion) so it
    can be unit-tested without live R2 or Postgres.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _R2_UPLOAD_ATTEMPTS + 1):
        try:
            exporter.upload_to_r2(local_file, object_key)
            return True, None
        except Exception as exc:
            last_exc = exc
            _logger.warning(
                "R2 upload attempt %d/%d failed: %s",
                attempt, _R2_UPLOAD_ATTEMPTS, str(exc)[:200],
            )
            if attempt < _R2_UPLOAD_ATTEMPTS:
                time.sleep(_R2_UPLOAD_BACKOFF * attempt)
    return False, last_exc


@app.task(
    name="src.workers.tasks.emit_payment_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def emit_payment_notification(self, user_id: str, attempt_count: int) -> None:
    """Best-effort in-app notification for a failed Stripe payment.

    Runs in the worker process so the notification insert uses the system role
    (the Stripe webhook is an API path with no user RLS GUC, and the API must
    never use system_sync_session)."""
    from src.workers.notification_emit import create_notification
    create_notification(
        user_id=user_id, type="payment_failed", job_id=None,
        detail={"attempt_count": attempt_count},
    )


def _fail_job_after_uncaught(job_id: str, reason: str, expected_started_at=None) -> None:
    """Last-resort terminal cleanup for a crashed run_scrape_job (see _RunScrapeJobTask).

    If an exception escapes run_scrape_job, the job is left pinned in a NON-terminal
    status (e.g. 'enriching') with no error message — indistinguishable from a hang and
    only recoverable by the watchdog's slow started_at fallback. This opens a FRESH
    system session (the task's own session is gone by the time on_failure runs) and
    terminalizes the job. Best-effort: it must never raise out of on_failure.

    ATTEMPT-SCOPED ownership (Codex P2 ×2):
    - REQUIRE the attempt token. `expected_started_at` is the started_at this worker
      stamped when it WON the pending→queued claim. If it's None the task crashed before
      claiming (allowlist refresh / redis / bootstrap) or is a stale duplicate delivery —
      it never owned the job, so we must NOT fail it (that could kill another live
      attempt). No token → no-op.
    - ATOMIC guard. The watchdog re-queues a stuck job by NULLing started_at
      (scheduler_helpers/health.py) and a replacement claim stamps a fresh started_at, so
      ownership can change between a SELECT-side check and the UPDATE. We therefore fail
      the row in ONE statement whose WHERE pins BOTH `started_at = :expected` AND a
      non-terminal status. A re-queued/re-claimed newer attempt (different or NULL
      started_at) matches 0 rows and is left untouched; the watchdog retry path is
      preserved. The failure log is published ONLY if this UPDATE actually terminalized
      the row.
    - NOT-YET-BILLED guard (Codex P2). Only terminalize a job that has not billed
      (`billing_applied_at IS NULL`). A crash AFTER billing committed (e.g. a transient
      redis/DB error in a later _publish_log / enrichment / delivery, before the final
      'done') must be left for the watchdog: its re-run skips the billing CAS (already
      applied) and drives the job to 'done', so the user isn't left charged-but-failed.
      The primary failure mode this hook targets — a crash in the insert/dedup phase —
      happens BEFORE billing, so billing_applied_at is NULL and it still fails cleanly.
    """
    if expected_started_at is None:
        return
    try:
        from sqlalchemy import update

        from src.db.models import Job
        from src.db.session import system_sync_session

        with system_sync_session() as db:
            row = db.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.started_at == expected_started_at,
                    Job.status.notin_(_TERMINAL_STATUSES),
                    Job.billing_applied_at.is_(None),
                )
                .values(status="failed", finished_at=_now(), error_message=reason)
                .returning(Job.user_id)
            ).fetchone()
            db.commit()
        if row is not None:
            r = _redis()
            _publish_log(r, job_id, "error", reason, db=None)
            r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
            _logger.error("Job %s failed (post-crash cleanup): %s", job_id, reason)
            # in-app notification (best-effort; gated by prefs inside the helper)
            from src.workers.notification_emit import create_notification
            create_notification(
                user_id=row[0], type="job_failed", job_id=job_id,
                detail={"error_summary": reason[:200]},
            )
    except Exception:  # cleanup must never mask or replace the original failure
        _logger.exception("Job %s: post-crash terminal cleanup failed", job_id)


class _RunScrapeJobTask(app.Task):
    """Custom base so an UNCAUGHT exception in run_scrape_job fails the job cleanly.

    run_scrape_job protects the scrape phase with try/except + _fail_job, but the
    post-scrape phase (insert / dedup / export / billing) is not fully wrapped; a crash
    there (e.g. the 2026-06-18 insertmanyvalues .rowcount AttributeError) escaped the task
    and left the job stuck in 'enriching'. on_failure fires in the worker once the task
    has raised its final exception (no self.retry() is used, so this IS the final
    outcome — Retry/soft-timeout included) and CAS-fails the job so it terminalizes with
    an error message instead of hanging.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Timeouts are RECOVERABLE, not crashes (Codex P2): a long scrape that blew the
        # soft/hard time_limit should go through the watchdog retry path (re-queue up to
        # max_retries), not be permanently failed on its first timeout. Only genuine
        # exceptions (which leave the job stuck non-terminal) terminalize here.
        if isinstance(exc, (SoftTimeLimitExceeded, TimeLimitExceeded)):
            return
        job_id = args[0] if args else (kwargs or {}).get("job_id")
        if job_id:
            # started_at of THIS attempt, stashed on the request by run_scrape_job right
            # after it WON the claim. None if the crash happened before the claim — the
            # helper then no-ops (we never owned the job, so we must not fail it).
            expected_started_at = getattr(self.request, "scrape_started_at", None)
            _fail_job_after_uncaught(
                str(job_id),
                "Job failed during processing — our team has been notified.",
                expected_started_at=expected_started_at,
            )


@app.task(
    name="src.workers.tasks.run_scrape_job",
    base=_RunScrapeJobTask,
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=3600,  # 60 min (scrape + enrichment in one job)
    time_limit=3900,       # 65 min
)
def run_scrape_job(self, job_id: str) -> None:
    """Execute a full scrape job lifecycle for the given job_id."""
    from sqlalchemy import func, select, update

    from src.api.middleware.security import register_connector_domains_from_db
    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import rls_sync_session, system_sync_session
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class
    from src.utils.data_exporter import DataExporter
    from src.utils.lead_export import resolve_hidden_output_fields
    from src.workers.delivery import deliver_job_email

    # Refresh the in-process SSRF allowlist from the connectors table before
    # scraping. A connector added through POST /scrapers/connectors after this
    # worker booted would otherwise still be missing from the frozenset that
    # validate_scraping_target() checks. Idempotent and cheap.
    register_connector_domains_from_db()

    r = _redis()

    # ── Bootstrap: look up user_id for this job_id without RLS ──────────────
    # We need the user_id BEFORE we can enter the RLS-scoped session, and
    # the Celery task only receives job_id. This bootstrap query is a
    # legitimate system operation — the Celery task was dispatched by the
    # API which already authorized this user, and the worker's role is to
    # act on their behalf. Loading the user_id by job_id cannot leak data:
    # the caller already knows the job_id they're asking about.
    with system_sync_session() as _boot:
        boot_row = _boot.execute(
            select(Job.user_id, Job.status).where(Job.id == job_id)
        ).first()
        if boot_row is None:
            _logger.error("Job %s not found — aborting", job_id)
            return
        _boot_user_id, _boot_status = boot_row
        if _boot_status == "cancelled":
            _logger.info("Job %s was cancelled before worker picked it up", job_id)
            return

    # Everything past this point runs with the RLS policies bound to
    # this job's user_id. Inserts into results, delivered_records,
    # pending_skip_trace_rows etc. are now scoped at the DB level as
    # well as the ORM level. H1 + C1 from the full-SaaS review.
    #
    # The HeartbeatThread context wraps the whole body so its stop() fires on
    # EVERY exit path — normal return, early return, OR uncaught exception — and
    # can't pin a non-terminal job "alive" past the work (the primary lifecycle;
    # self-reap + lifetime cap are backups). It is constructed here but only
    # .start()ed after the claim below, so a job this worker doesn't own never
    # gets a heartbeat.
    with HeartbeatThread(job_id) as _hb, rls_sync_session(_boot_user_id) as db:
        # ── Load job ─────────────────────────────────────────────────────────
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job is None:
            _logger.error("Job %s disappeared between bootstrap and load", job_id)
            return

        if job.status == "cancelled":
            _logger.info("Job %s was cancelled before worker picked it up", job_id)
            return

        config = db.execute(
            select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)
        ).scalar_one()

        user = db.execute(select(User).where(User.id == job.user_id)).scalar_one()

        # ── QUEUED (atomic claim) ─────────────────────────────────────────────
        # Compare-and-set pending->queued so a duplicate delivery of this job_id
        # can't double-scrape. A duplicate can arrive from Celery redelivery OR a
        # recovery re-enqueue of a child still in 'pending' (Track A). Only the
        # worker that flips the row FROM 'pending' proceeds; rowcount 0 means
        # another worker already owns it (or it was cancelled / already running),
        # so we return without scraping. Every dispatch path (API trigger,
        # scheduler, watchdog re-queue, batch fan-out) enqueues a 'pending' job,
        # so this never rejects a legitimate first delivery.
        #
        # TRADEOFF (Codex P2, accepted): tasks are acks_late=True, so a worker
        # killed AFTER this commit but before the broker ack triggers a
        # redelivery. The pending-only guard makes that redelivery a no-op (the
        # row is no longer 'pending'). Recovery of such an abandoned in-flight job
        # is therefore owned by watchdog_stuck_jobs (re-queues stuck queued/
        # scraping rows at 10-20 min), NOT the immediate acks_late path. We accept
        # the slower recovery to GUARANTEE no concurrent double-scrape — the old
        # blind set gave fast redelivery recovery only by also double-running
        # genuine duplicates. A per-job lease would buy back the fast path; out of
        # scope here and unnecessary (the batch barrier waits for terminal
        # children regardless of which recovery path fires).
        # ROLLBACK 2026-06-18: the HeartbeatThread is DISABLED (see below) because it
        # deadlocked the insert phase contending for the small worker connection pool
        # (every post-PR#59 scrape wedged at "Saving records to database..."). We
        # deliberately do NOT stamp last_heartbeat_at here, so it stays NULL and the
        # watchdog uses its conservative started_at>stuck_cutoff fallback (the proven
        # PR#57 behavior). Re-enable the heartbeat only with a dedicated NullPool
        # engine isolated from the work pool (see docs/BUILD_JOURNAL.md).
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "pending")
            .values(status="queued", started_at=_now())
        ).rowcount
        db.commit()
        if not claimed:
            _logger.info(
                "Job %s not claimable (already in flight / not pending) — "
                "skipping to avoid double-scrape",
                job_id,
            )
            return
        db.refresh(job)
        # Record THIS attempt's started_at on the Celery request so the on_failure hook
        # (_RunScrapeJobTask) can attempt-scope its crash cleanup — it must only fail the
        # row if started_at still matches, never a re-queued/re-claimed newer attempt.
        try:
            self.request.scrape_started_at = job.started_at
        except Exception:  # request context unavailable (e.g. direct call) — non-fatal
            pass

        # Execution-time entitlement backstop (audit until ENTITLEMENT_ENFORCEMENT).
        # Catches API/scheduled/retry/watchdog paths that bypassed create-time checks.
        # IMPORTANT: runs AFTER the ownership CAS (pending->queued) so that only the
        # owning worker can act — a duplicate/redelivered task would have returned at
        # `if not claimed` above and never reach this guard.
        from src.api.entitlements import ConfigRow, config_run_violation, should_block_run
        _active = db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
            ).where(ScraperConfig.user_id == job.user_id, ScraperConfig.active)
        ).all()
        _violation = config_run_violation(
            user.plan, config.state, config.county, config.record_type,
            [ConfigRow(*r) for r in _active],
        )
        if should_block_run(_violation, user_id=str(job.user_id), plan=(user.plan or "starter"), context="worker_run"):
            _publish_log(r, job_id, "error", f"Plan limit — {_violation}", db=db)
            _fail_job(db, job, r, job_id, f"Plan limit reached: {_violation}")
            return

        # Liveness heartbeat DISABLED (rollback 2026-06-18). The daemon thread shared
        # the worker's small sync connection pool (pool_size=2) with the main work
        # session + _publish_log; during the DB-heavy insert phase the main thread and
        # the heartbeat thread deadlocked on the pool, wedging EVERY scrape at the
        # insert with a frozen heartbeat (worker-internal, invisible to pg_stat_activity;
        # DB/insert/commit all verified fast in isolation). Leaving it unstarted (the
        # `with HeartbeatThread(...)` __enter__ never starts it; __exit__.stop() no-ops)
        # restores scraping and reverts the watchdog to its started_at fallback. Re-enable
        # ONLY with a dedicated NullPool engine for the heartbeat (Codex; BUILD_JOURNAL).
        # _hb.start(job.started_at)  # DISABLED — do not re-enable without pool isolation
        _publish_log(r, job_id, "info", f"Job queued — {config.name} ({config.county}, {config.state})", db=db)

        # ── PROBING ───────────────────────────────────────────────────────────
        if not _set_status(db, job, "probing"):
            _logger.info("Job %s externally terminalized (%s) — aborting", job_id, job.status)
            return
        _publish_log(r, job_id, "info", "Probing county portal...", db=db)

        try:
            scraper_class, matched_record_type = get_scraper_class(config.county, config.state, config.record_type)
        except UnsupportedCountyError as exc:
            reason = str(exc)
            if _fail_job(db, job, r, job_id, reason):
                from src.workers.notification_emit import create_notification
                create_notification(
                    user_id=job.user_id, type="job_failed", job_id=job_id,
                    detail={
                        "scraper_name": getattr(config, "name", None),
                        "county": getattr(config, "county", None),
                        "error_summary": reason[:200],
                    },
                )
            return

        # ── SCRAPING ──────────────────────────────────────────────────────────
        if not _set_status(db, job, "scraping"):
            _logger.info("Job %s externally terminalized (%s) — aborting", job_id, job.status)
            return
        record_label = config.record_type.replace("_", " ").title()
        _publish_log(r, job_id, "success", f"Starting scrape — {record_label} records", db=db)

        from typing import cast

        from src.api.schemas import ScheduleConfigDict
        schedule: ScheduleConfigDict = cast(ScheduleConfigDict, config.schedule or {})
        range_mode = schedule.get("date_range_mode") or schedule.get("range_mode", "rolling_90")  # type: ignore[call-overload]  # legacy "range_mode" alias kept for old configs
        date_from, date_to = _resolve_date_range(schedule, config_id=config.id, job_id=job_id, user_plan=user.plan, record_type=config.record_type)

        # Enforce per-connector max date range (e.g. Chelan single-date = 30 days max).
        # Look up the connector to get the limit.
        from src.db.models import CountyConnector
        connector = db.execute(
            select(CountyConnector).where(
                func.lower(CountyConnector.county) == config.county.lower(),
                func.upper(CountyConnector.state) == config.state.upper(),
                CountyConnector.active,
            )
        ).scalars().first()
        max_days = connector.max_date_range_days if connector else None
        if max_days:
            from datetime import timedelta as _td
            _df = datetime.strptime(date_from, "%m/%d/%Y")
            _dt = datetime.strptime(date_to, "%m/%d/%Y")
            actual_days = (_dt - _df).days
            if actual_days > max_days:
                # Trim date_from to respect the limit (keep the most recent data)
                _df = _dt - _td(days=max_days)
                date_from = _df.strftime("%m/%d/%Y")
                _publish_log(
                    r, job_id, "warning",
                    f"{config.county.title()} County supports max {max_days} days. Range trimmed to {date_from} → {date_to}.",
                    db=db,
                )

        job.date_from = date_from
        job.date_to = date_to
        db.flush()
        _publish_log(r, job_id, "info", f"Date range: {date_from} → {date_to} (mode: {range_mode})", db=db)

        _last_phase = [None]  # mutable for closure

        def _on_progress(page_current, page_total, record_count, phase="scraping"):
            """Called by the scraper after each page — updates the DB in real time."""
            job.page_current = page_current
            job.page_total = page_total
            job.record_count = record_count
            try:
                db.commit()
            except Exception:
                # DB connection may have gone stale during long scrape — reconnect
                try:
                    db.rollback()
                    db.commit()
                except Exception:
                    _logger.warning("Progress commit failed — will retry on next update")

            # Log phase transitions so the frontend shows what's happening
            if phase != _last_phase[0]:
                _last_phase[0] = phase
                if phase == "parcel_lookup":
                    _publish_log(r, job_id, "info", f"Looking up parcel IDs from detail pages ({page_total} records)...", db=db)
                elif phase == "enriching":
                    _publish_log(r, job_id, "info", f"Looking up addresses for {page_total} parcels...", db=db)

        _publish_log(r, job_id, "info", "Connecting to county portal...", db=db)
        # Update progress label so the live page shows activity during captcha solve.
        # L7 (full-SaaS review): the previous "rollback() then commit()"
        # recovery dance is fragile — unpack it into explicit branches
        # so the intent is grep-friendly.
        job.progress_label = "Connecting to portal..."
        try:
            db.commit()
        except Exception as commit_exc:
            _logger.warning(
                "Failed to commit progress_label for job %s: %s",
                job_id, str(commit_exc)[:120],
            )
            try:
                db.rollback()
            except Exception:
                pass

        try:
            # Wrap scraper in a 30-minute timeout so a hung Playwright
            # session doesn't burn the full 60-min Celery soft_time_limit.
            _SCRAPE_TIMEOUT = 1800  # 30 minutes
            records = asyncio.run(
                asyncio.wait_for(
                    _run_scraper(scraper_class, date_from, date_to, r, job_id, _on_progress, record_type=matched_record_type, doc_types=config.doc_types),
                    timeout=_SCRAPE_TIMEOUT,
                )
            )
        except TimeoutError:
            _logger.error("Scraper timed out after %ds for job %s", _SCRAPE_TIMEOUT, job_id)
            try:
                db.rollback()
            except Exception:
                pass
            reason = f"Scraper timed out after {_SCRAPE_TIMEOUT // 60} minutes. Try a shorter date range."
            if _fail_job(db, job, r, job_id, reason):
                from src.workers.notification_emit import create_notification
                create_notification(
                    user_id=job.user_id, type="job_failed", job_id=job_id,
                    detail={
                        "scraper_name": getattr(config, "name", None),
                        "county": getattr(config, "county", None),
                        "error_summary": reason[:200],
                    },
                )
            return
        except Exception as exc:
            _logger.exception("Scraper error for job %s", job_id)
            # Capture THIS attempt's started_at BEFORE the rollback — rollback
            # expires ORM attributes, and a re-fetch could return a NEWER attempt's
            # value. It attempt-scopes BOTH the retry CAS and the terminal fail
            # below so a stale/superseded attempt never clobbers a live re-claimed
            # one (Codex P1).
            attempt_started_at = job.started_at
            # Reconnect DB session if it went stale during long scrape
            try:
                db.rollback()
            except Exception:
                pass
            # Transient portal hiccup (page never rendered, pagination flaked, block
            # wall, Playwright timeout) → re-queue this job with backoff instead of
            # permanently failing the whole day's scrape on ONE flaky page. Bounded by
            # SCRAPE_TRANSIENT_MAX_RETRIES; once exhausted (or a PERMANENT error) we
            # fall through and fail loud as before. Billing has NOT run at this point,
            # so a re-run cannot double-bill (guarded inside _retry_scrape_job).
            from src.scrapers.reliability import is_transient_scrape_error
            if is_transient_scrape_error(exc):
                countdown = _retry_scrape_job(
                    db, job, job_id, attempt_started_at,
                    max_retries=SCRAPE_TRANSIENT_MAX_RETRIES,
                    backoffs=SCRAPE_TRANSIENT_BACKOFF_SECONDS,
                )
                if countdown is not None:
                    queue = (
                        "scrape-priority"
                        if user.plan in PRIORITY_QUEUE_PLANS
                        else "scrape"
                    )
                    try:
                        run_scrape_job.apply_async(
                            args=[job_id], queue=queue, countdown=countdown
                        )
                    except Exception:
                        # Broker publish failed — the row is durably 'pending' with
                        # retry_count>0 and started_at NULL, which watchdog_stuck_jobs
                        # re-delivers via its stranded-retry branch (retry_count>0,
                        # started_at IS NULL) once the row ages past the stuck cutoff.
                        # Recovery is bounded (not immediate), but no retry is lost.
                        _logger.warning(
                            "run_scrape_job retry publish failed for job %s; left "
                            "'pending' for watchdog re-delivery", job_id, exc_info=True,
                        )
                    _publish_log(
                        r, job_id, "warning",
                        f"Transient error — retrying in ~{max(1, countdown // 60)} min "
                        f"(retry {job.retry_count} of {SCRAPE_TRANSIENT_MAX_RETRIES}).",
                        db=db,
                    )
                    _logger.warning(
                        "Job %s: transient scrape error — re-queued (retry %d/%d, "
                        "countdown %ds): %s",
                        job_id, job.retry_count, SCRAPE_TRANSIENT_MAX_RETRIES,
                        countdown, str(exc)[:200],
                    )
                    return
            reason = "Scraper encountered an error — our team has been notified."
            # Attempt-scoped: only fail the job if THIS attempt still owns it
            # (started_at unchanged). If a newer attempt re-claimed it — or the
            # retry CAS above no-oped on an ownership change — this no-ops instead
            # of terminalizing a live newer attempt (Codex P1).
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_started_at):
                from src.workers.notification_emit import create_notification
                create_notification(
                    user_id=job.user_id, type="job_failed", job_id=job_id,
                    detail={
                        "scraper_name": getattr(config, "name", None),
                        "county": getattr(config, "county", None),
                        "error_summary": reason[:200],
                    },
                )
            return

        _publish_log(r, job_id, "success", f"Scrape complete — {len(records)} records found", db=db)

        # ── Phase 3: honest probate output ────────────────────────────────────
        # Drop LIVING-owner Transfer-on-Death estate-planning deeds unless the
        # customer opted in (include_living_owner_tod is False = new probate
        # default; NULL = grandfathered → keep; True = explicit opt-in → keep).
        # Done ONCE here, before the plan-quota cap / DB insert / in-memory R2
        # export / counts — all of which derive from `records` — so a filtered
        # row never reaches persistence, export, dedup, enrichment, billing, or
        # property membership (the first export is built from this in-memory list,
        # not persisted rows — Codex). Death-triggered TOD (a recorder comment
        # carries the death marker) is kept by should_include_probate_row.
        if config.record_type == "probate" and config.include_living_owner_tod is False:
            _before_tod = len(records)
            records = [
                rec for rec in records
                if should_include_probate_row(
                    "probate", False, rec.doc_type,
                    (rec.enrichment_data or {}).get("comment"),
                )
            ]
            _dropped_tod = _before_tod - len(records)
            if _dropped_tod:
                _publish_log(
                    r, job_id, "info",
                    f"Excluded {_dropped_tod} living-owner Transfer-on-Death "
                    "estate-planning record(s) per scraper settings.",
                    db=db,
                )

        # ── Cap records to user's remaining plan quota ────────────────────────
        if user.records_limit != -1:
            remaining = max(0, user.records_limit - (user.records_used or 0))
            if remaining < len(records):
                _publish_log(
                    r, job_id, "warning",
                    f"Plan limit: saving {remaining} of {len(records)} records. Upgrade for more.",
                    db=db)
                records = records[:remaining]

        # ── ENRICHING ─────────────────────────────────────────────────────────
        # CAS no-op here means a batch force-finalize cancelled this child while
        # it was scraping (>90min stuck): discard the scrape without saving,
        # billing, or delivering — the batch already recorded it as timed out.
        if not _set_status(db, job, "enriching", record_count=len(records)):
            _logger.info(
                "Job %s externally terminalized (%s) mid-scrape — discarding without billing",
                job_id, job.status,
            )
            return
        _publish_log(r, job_id, "info", "Saving records to database...", db=db)

        # Bulk insert results (truncate fields to fit DB column limits)
        def _trunc(val: str | None, max_len: int) -> str | None:
            return val[:max_len] if val and len(val) > max_len else val

        import hashlib
        import re as _re
        import uuid as _uuid

        def _compute_dedup_hash(
            parcel_id: str | None,
            property_address: str | None,
            party_name: str | None = None,
            date_recorded: str | None = None,
        ) -> str | None:
            """Sprint 6.4 dedup key. Strong branch is the FROZEN
            legacy_strong_signature (parcel|address) — this keys
            delivered_records (BILLING dedup) and must never change scheme.
            It deliberately DIVERGED from the overlap property_key on
            2026-06-12 (see property_identity.py). Fallback unchanged."""
            strong = _legacy_strong_signature(parcel_id, property_address)
            if strong is not None:
                return strong
            # Fallback: party_name + date_recorded (unchanged)
            name = (party_name or "").strip().upper()
            name = _re.sub(r"\s+", " ", name).strip()
            date = (date_recorded or "").strip()
            if len(name) >= 3 and len(date) >= 6:
                key = f"NAME:{name}|DATE:{date}"
                return hashlib.sha256(key.encode("utf-8")).hexdigest()
            return None

        # Bulk insert with ON CONFLICT DO NOTHING on the per-job idempotency key
        # (job_id, source_fingerprint) so a watchdog re-run of this SAME job
        # re-inserts the same rows as no-ops instead of APPENDING a second copy
        # (the 2026-06-17 duplication incident). pg_insert is required for the
        # ON CONFLICT clause; the plain core insert can't express it.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        def _source_fingerprint(rec) -> str:
            """Stable within-job idempotency key from the record's SCRAPE-TIME
            source identity ONLY. Deliberately EXCLUDES enrichment_data and
            mailing_address: those are filled / re-normalized during enrichment,
            so hashing the full record (make_hash(to_dict())) could yield a
            DIFFERENT key on a re-run and append a duplicate instead of conflicting
            (Codex). SHA-256 of a canonical field tuple; genuinely-distinct records
            (incl. multiple filings per parcel) keep distinct tuples, so ON CONFLICT
            never collapses a legitimate row."""
            parts = (
                config.record_type or "",
                (rec.parcel_id or "").strip(),
                (rec.date_recorded or "").strip(),
                (rec.doc_type or "").strip(),
                (rec.party_name or "").strip(),
                (rec.legal_description or "").strip(),
                (rec.property_address or "").strip(),
            )
            return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

        # Product invariant (BACKLOG §9): a tax_delinquent record set may only be
        # persisted if EVERY row is from a qualified tax source AND carries both
        # delinquent_amount + bill_year. Validate the WHOLE set before the batched
        # insert loop below — a violation raises and fails the job atomically
        # (on_failure → status=failed), so a mislabeled deed can never be written
        # as a tax lead (the Clark 2026-04 incident). No-op for non-tax types.
        validate_tax_delinquent_records(records, config.record_type)

        batch_size = 1000
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            rows = []
            for rec in batch:
                # Phase 4: structured tax fields (King tax_delinquent only).
                _tax_amount, _tax_bill_year = _extract_tax_fields(
                    rec.enrichment_data, config.record_type
                )
                # Within-job idempotency key (migration 062). Reuse the scraper's
                # raw_html_hash when set — it is the scraper's OWN stable in-memory
                # dedup key (recomputed identically on a re-run). For scrapers that
                # don't set it (e.g. King Socrata tax), fall back to a canonical
                # scrape-time identity tuple. Both are stable across re-runs, so
                # ON CONFLICT skips an already-present row instead of appending.
                _fingerprint = rec.raw_html_hash or _source_fingerprint(rec)
                # Tier 0 (057): best-effort owner-location flags at insert. mailing
                # is usually NULL pre-enrichment (so absentee/out_of_state come back
                # NULL here); the end-of-job recompute after _run_inline_enrichment
                # is the authoritative pass once mailing is filled.
                _owner = compute_owner_flags(rec.property_address, rec.mailing_address)
                # Honesty label (probate only): tag every probate row with its signal
                # subtype so a LIVING-owner Transfer-on-Death deed is never delivered
                # disguised as a death/inheritance lead. New dict (never mutate the
                # scraper's record); EXCLUDED from source_fingerprint/dedup_hash above,
                # so labeling cannot affect identity, dedup, or billing.
                _enrichment = rec.enrichment_data or {}
                if config.record_type == "probate":
                    # doc_type-primary, recorder-COMMENT fallback (Skagit stores the
                    # probate signal in the comment, not doc_type — Codex P2).
                    _subtype = classify_probate_signal_for_row(
                        rec.doc_type, _enrichment.get("comment")
                    )
                    _enrichment = {**_enrichment, "lead_subtype": _subtype.value}
                rows.append({
                    "id": str(_uuid.uuid4()),
                    "job_id": job_id,
                    "user_id": job.user_id,
                    "date_recorded": _trunc(rec.date_recorded, 32),
                    "party_name": _trunc(rec.party_name, 512),
                    "heirs": rec.heirs,
                    "legal_description": rec.legal_description,
                    "doc_type": _trunc(rec.doc_type, 128),
                    "parcel_id": _trunc(rec.parcel_id, 64),
                    "property_address": _trunc(rec.property_address, 512),
                    "mailing_address": _trunc(rec.mailing_address, 512),
                    "enrichment_data": _enrichment,
                    "raw_html_hash": rec.raw_html_hash,
                    # Migration 062: per-job idempotency key (ON CONFLICT target).
                    "source_fingerprint": _fingerprint,
                    # Sprint 6.4: dedup hash computed now, duplicate flag
                    # resolved in the post-insert dedup scan below
                    "dedup_hash": _compute_dedup_hash(rec.parcel_id, rec.property_address, rec.party_name, rec.date_recorded),
                    "is_duplicate": False,
                    # Phase 4: NULL for everything except King structured tax rows.
                    "delinquent_amount": _tax_amount,
                    "delinquent_bill_year": _tax_bill_year,
                    # Tier 0 (057): owner-location flags (mostly recomputed post-enrich).
                    "property_state": _owner["property_state"],
                    "owner_state": _owner["owner_state"],
                    "absentee_owner": _owner["absentee_owner"],
                    "out_of_state_owner": _owner["out_of_state_owner"],
                })
            # ON CONFLICT on the partial unique index (job_id, source_fingerprint)
            # WHERE source_fingerprint IS NOT NULL — every row here has a non-null
            # fingerprint, so a re-run's already-present rows are skipped (rowcount
            # counts only genuinely-new rows). index_where MUST match the partial
            # index predicate or Postgres won't use it as the conflict arbiter.
            stmt = pg_insert(Result).on_conflict_do_nothing(
                index_elements=["job_id", "source_fingerprint"],
                index_where=sa_text("source_fingerprint IS NOT NULL"),
            )
            # Executed with a list of rows, pg_insert(...).on_conflict_do_nothing()
            # routes through SQLAlchemy insertmanyvalues, whose IteratorResult has NO
            # .rowcount — reading it raises AttributeError and crashed EVERY scrape
            # post-PR#59 (job left stuck in 'enriching'; 2026-06-18). We don't need a
            # per-batch insert count: the authoritative persisted count is the dedup
            # SELECT below (and billing counts persisted non-dup rows, not rowcount).
            db.execute(stmt, rows)
            db.commit()

        # ── SPRINT 6.4: CROSS-JOB DEDUPLICATION ────────────────────────────
        # For each newly-inserted Result that has a dedup_hash, try to
        # INSERT into delivered_records. PostgreSQL's ON CONFLICT DO
        # NOTHING tells us which rows were successfully claimed (first
        # delivery) vs which conflicted (user has seen this lead before).
        # The conflicting rows get their Result flagged is_duplicate=true.
        _publish_log(r, job_id, "info", "Checking for duplicate leads...", db=db)
        _logger.info("Job %s: dedup step 1 — SELECT fresh rows", job_id)

        # Step 1: pull the freshly-inserted results back so we have their
        # Result.id for the first_result_id foreign key
        fresh_rows = db.execute(
            sa_text("""
                SELECT id, dedup_hash, parcel_id, property_address
                FROM results
                WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) AND dedup_hash IS NOT NULL
            """),
            {"jid": job_id, "uid": str(job.user_id)},
        ).fetchall()

        _logger.info("Job %s: dedup step 1 done — %d fresh rows", job_id, len(fresh_rows))
        dup_count = 0
        unique_count = 0
        if fresh_rows:
            _logger.info("Job %s: dedup step 2 — INSERT delivered_records", job_id)
            # Step 2: single batched upsert into delivered_records.
            # ON CONFLICT DO NOTHING is the atomic "claim first delivery"
            # primitive — the unique (user_id, dedup_hash) constraint
            # guarantees exactly one winner per lead per user.
            # RETURNING tells us which hashes were actually inserted
            # (the "first delivery" ones) so we can derive duplicates
            # via set difference.
            insert_payload = [
                {
                    "id": str(_uuid.uuid4()),
                    "user_id": str(job.user_id),
                    "dedup_hash": row.dedup_hash,
                    "first_result_id": str(row.id),
                    "first_job_id": job_id,
                    "parcel_id": row.parcel_id,
                    "property_address": row.property_address,
                }
                for row in fresh_rows
            ]
            # Batch in groups of 500 to keep the SQL statement reasonable
            claimed_hashes: set[str] = set()
            for j in range(0, len(insert_payload), 500):
                chunk = insert_payload[j:j + 500]
                values_sql = ",".join(
                    f"(:id_{k}, :user_id_{k}, :dedup_hash_{k}, :first_result_id_{k}, "
                    f":first_job_id_{k}, :parcel_id_{k}, :property_address_{k}, NOW())"
                    for k in range(len(chunk))
                )
                params = {}
                for k, c in enumerate(chunk):
                    params[f"id_{k}"] = c["id"]
                    params[f"user_id_{k}"] = c["user_id"]
                    params[f"dedup_hash_{k}"] = c["dedup_hash"]
                    params[f"first_result_id_{k}"] = c["first_result_id"]
                    params[f"first_job_id_{k}"] = c["first_job_id"]
                    params[f"parcel_id_{k}"] = c["parcel_id"]
                    params[f"property_address_{k}"] = c["property_address"]

                result = db.execute(
                    sa_text(f"""
                        INSERT INTO delivered_records
                            (id, user_id, dedup_hash, first_result_id,
                             first_job_id, parcel_id, property_address,
                             first_delivered_at)
                        VALUES {values_sql}
                        ON CONFLICT (user_id, dedup_hash) DO NOTHING
                        RETURNING dedup_hash
                    """),
                    params,
                )
                for row in result.fetchall():
                    claimed_hashes.add(row.dedup_hash)
            _logger.info("Job %s: dedup step 2 INSERT done — committing", job_id)
            db.commit()
            _logger.info("Job %s: dedup step 2 committed — %d claimed", job_id, len(claimed_hashes))

            # Step 2b (idempotent re-run): claims this job already owns from a PRIOR
            # attempt (first_job_id = this job) conflict on the ON CONFLICT above so
            # they're absent from RETURNING — without this, a watchdog re-run would
            # mark every already-claimed row is_duplicate=true and "deliver" an
            # all-duplicate empty result. Treat hashes THIS job already owns as
            # first-delivery (mine), not duplicates. New attempts on a fresh job
            # return nothing here, so this is a no-op on the normal path.
            owned = db.execute(
                sa_text(
                    "SELECT dedup_hash FROM delivered_records "
                    "WHERE first_job_id = :jid AND user_id = CAST(:uid AS uuid)"
                ),
                {"jid": job_id, "uid": str(job.user_id)},
            ).fetchall()
            for row in owned:
                claimed_hashes.add(row.dedup_hash)

            # Step 3: any fresh Result whose dedup_hash is NOT in claimed_hashes
            # was a duplicate. Mark those rows.
            duplicate_result_ids = [
                str(row.id) for row in fresh_rows
                if row.dedup_hash not in claimed_hashes
            ]
            unique_count = len(claimed_hashes)
            dup_count = len(duplicate_result_ids)

            if duplicate_result_ids:
                # Batch the UPDATE to avoid an IN clause explosion.
                # Cast text[] to uuid[] — results.id is UUID type but
                # duplicate_result_ids are Python strings. Without the
                # cast, Postgres raises "operator does not exist: uuid = text".
                for j in range(0, len(duplicate_result_ids), 500):
                    chunk = duplicate_result_ids[j:j + 500]
                    db.execute(
                        sa_text(
                            "UPDATE results SET is_duplicate = true "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": chunk},
                    )
                db.commit()

        _publish_log(
            r, job_id, "success",
            f"{len(records)} records saved ({unique_count} new leads, {dup_count} duplicates)",
            db=db,
        )

        # ── EXPORT ────────────────────────────────────────────────────────────
        from src.api.schemas import DeliverConfigDict
        deliver_config: DeliverConfigDict = cast(DeliverConfigDict, config.deliver or {})
        # Honor the user's chosen export format. DeliverConfig stores
        # `formats: list[str]`. We export the first format in the list;
        # if a user selected multiple, only the first is generated for
        # now (multi-format export is a separate feature). An empty or
        # missing list falls back to CSV. Previously the worker read a
        # `format` (singular) key that schemas.DeliverConfig never sets,
        # so every export silently came out as CSV regardless of the
        # user's selection — flagged by Codex adversarial review.
        from src.config.constants import (
            DEFAULT_EXPORT_FORMAT,
            SUPPORTED_EXPORT_FORMATS,
        )
        formats = deliver_config.get("formats") or [DEFAULT_EXPORT_FORMAT]
        fmt = formats[0]
        # Belt to the schema validator's suspenders: a config saved BEFORE the
        # formats allowlist landed (or via any path that skips validation) can
        # still hold an unsupported value. Coerce it to the default here rather
        # than let DataExporter.export() raise and fail every scrape for that
        # config (Codex). New saves are rejected up front by bound_formats.
        if fmt.lower() not in SUPPORTED_EXPORT_FORMATS:
            _logger.warning(
                "Job %s: unsupported export format %r on config %s — falling back to %s",
                job_id, fmt, getattr(config, "id", "?"), DEFAULT_EXPORT_FORMAT,
            )
            fmt = DEFAULT_EXPORT_FORMAT

        _publish_log(r, job_id, "info", f"Building {fmt.upper()} export...", db=db)

        record_dicts = [r_obj.to_dict() for r_obj in records]
        # Honor the user's output-field visibility (blank deselected hideable
        # columns; identity/derived columns always present). Legacy/empty => all.
        hidden_fields = resolve_hidden_output_fields(config.fields)
        exporter = DataExporter()
        local_file = exporter.export(
            record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt, hidden_fields=hidden_fields
        )

        object_key = f"exports/{job.user_id}/{job_id}/leads.{local_file.suffix.lstrip('.')}"
        # Upload the deliverable to R2 with a few retries (transient R2 blips are
        # common). A FAILED upload is NOT non-fatal: the local file is deleted in
        # `finally`, and BOTH delivery paths (email + in-app download) require
        # object_key — so a swallowed failure marked the job done+billed with no
        # deliverable anywhere, stranding a paying user (Codex High). Treat "no
        # deliverable" as a job FAILURE instead (see the not-upload_ok branch).
        try:
            upload_ok, upload_exc = _upload_export_with_retry(
                exporter, local_file, object_key
            )
        finally:
            local_file.unlink(missing_ok=True)

        if upload_ok:
            _publish_log(r, job_id, "success", "Export uploaded to cloud storage", db=db)
        else:
            # No deliverable produced. Release this job's cross-job dedup claims
            # (committed at the dedup step BEFORE export) so the never-delivered,
            # unbilled leads are not treated as duplicates on a future re-scrape
            # (Codex). Then fail loudly: billing has NOT run yet (it's below), so
            # the user is not charged, and a FAILED job is visible + retryable.
            _logger.error(
                "Job %s: R2 upload failed after %d attempts — failing job (no deliverable): %s",
                job_id, _R2_UPLOAD_ATTEMPTS, str(upload_exc)[:200],
            )
            try:
                db.rollback()
                # Tenant-scoped DELETE (user_id alongside first_job_id) per the
                # repo's mandatory user_id-filter rule. NOTE: this needs DELETE on
                # delivered_records for the worker role; granted to
                # bridgeleads_system in provision_rls_roles.sql. Works today (prod
                # role still BYPASSRLS); the grant covers the RLS cutover.
                db.execute(
                    sa_text(
                        "DELETE FROM delivered_records "
                        "WHERE first_job_id = :jid AND user_id = CAST(:uid AS uuid)"
                    ),
                    {"jid": job_id, "uid": str(job.user_id)},
                )
                db.commit()
            except Exception as cleanup_exc:
                db.rollback()
                _logger.error(
                    "Job %s: failed to release dedup claims after upload failure: %s",
                    job_id, str(cleanup_exc)[:200],
                )
            # Honest message: a FAILED job is terminal — the watchdog does NOT
            # re-queue it (it only requeues stuck active/pending jobs). A
            # scheduled scraper makes a fresh job on its next occurrence; a manual
            # run must be re-triggered by the user. Don't promise auto-retry.
            reason = (
                "Export upload to cloud storage failed after multiple attempts — "
                "no file was produced and you were not charged. Please run the "
                "scraper again; contact support if it keeps failing."
            )
            if _fail_job(db, job, r, job_id, reason):
                from src.workers.notification_emit import create_notification
                create_notification(
                    user_id=job.user_id, type="job_failed", job_id=job_id,
                    detail={
                        "scraper_name": getattr(config, "name", None),
                        "county": getattr(config, "county", None),
                        "error_summary": reason[:200],
                    },
                )
            return

        # Force-finalize guard (Codex P2): a batch force-finalize may have
        # cancelled this child while it was exporting. Re-check the live DB
        # status before charging quota — never bill a job that is no longer
        # ours to complete. (A cancel landing between this check and the final
        # done-CAS still can't resurrect the job; at worst that sliver of a
        # window bills records that were genuinely scraped.)
        db.refresh(job)
        if job.status in _TERMINAL_STATUSES:
            _logger.info(
                "Job %s externally terminalized (%s) after export — skipping billing/delivery",
                job_id, job.status,
            )
            return

        # Atomic update of monthly record usage.
        # Sprint 6.4: duplicates delivered to this user in a prior scrape
        # do NOT count against the monthly quota. Records without a
        # dedup_hash (no parcel AND no address) still count, because
        # they are genuinely new data even though we can't dedupe them.
        #
        # Bill the PERSISTED billable set, not len(records): with conflict-skipping
        # inserts the in-memory scrape count can diverge from what actually landed
        # (intra-run fingerprint collisions, a re-run over a changed source set), so
        # the authoritative billable count is this job's non-duplicate result rows
        # (no-dedup_hash rows have is_duplicate=false, so they're included). (Codex)
        billable_count = db.execute(
            sa_text(
                "SELECT count(*) FROM results "
                "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) AND is_duplicate = false"
            ),
            {"jid": job_id, "uid": str(job.user_id)},
        ).scalar() or 0
        from sqlalchemy import update as sa_update
        # Idempotent billing (migration 063): claim billing for THIS job via a CAS
        # on billing_applied_at. Only the attempt that flips it from NULL bills the
        # user, so a watchdog re-run (which re-reaches this point) never
        # double-charges records_used. billed_count records the charged amount. The
        # Job CAS + the User increment commit together (a crash between the two
        # execute()s rolls both back — neither is committed until db.commit()).
        billed_now = db.execute(
            sa_update(Job)
            .where(Job.id == job_id, Job.billing_applied_at.is_(None))
            .values(billed_count=billable_count, billing_applied_at=_now())
        ).rowcount
        if billed_now:
            user_billed = db.execute(
                sa_update(User)
                .where(User.id == user.id)
                .values(records_used=User.records_used + billable_count)
            ).rowcount
            if user_billed != 1:
                # The job was CAS-marked billed but the user counter did NOT move
                # (deleted user / bad id / RLS scope). Don't leave the job marked
                # billed-without-charge — roll back and fail loudly (Codex).
                db.rollback()
                reason = "Billing failed: user record-usage counter could not be updated."
                if _fail_job(db, job, r, job_id, reason):
                    from src.workers.notification_emit import create_notification
                    create_notification(
                        user_id=job.user_id, type="job_failed", job_id=job_id,
                        detail={
                            "scraper_name": getattr(config, "name", None),
                            "county": getattr(config, "county", None),
                            "error_summary": reason[:200],
                        },
                    )
                return
        else:
            _logger.info(
                "Job %s already billed (billing_applied_at set) — skipping "
                "records_used increment on this re-run", job_id,
            )
        db.commit()
        db.refresh(user)

        if user.records_limit != -1 and user.records_used > user.records_limit:
            overage = user.records_used - user.records_limit
            _publish_log(r, job_id, "warning", f"Plan limit exceeded by {overage} records. Upgrade to keep scraping.", db=db)

        # ── INLINE ENRICHMENT (BEFORE marking done) ──────────────────────────
        # Runs on this Celery task's thread, sharing the same DB session.
        # Earlier this code wrapped the call in a ThreadPoolExecutor with a
        # 5-minute future.result(timeout=...) so a hanging ArcGIS/assessor
        # HTTP request couldn't stall the worker forever. Two issues: (1)
        # the `with ThreadPoolExecutor(...) as executor:` exit waits for
        # the worker thread to finish, so the timeout never actually freed
        # the Celery worker on a real hang. (2) Passing the caller's
        # SQLAlchemy session across threads is unsafe and can corrupt
        # transaction state under concurrency. Both flagged by Codex
        # adversarial review. The Celery task's time_limit=3900s remains
        # the real hard cap; per-HTTP-call timeouts inside the enrichment
        # helpers (county_gis / king_county_assessor / etc) bound each
        # request's wait. If a hang slips through both, Celery hard-kills
        # the worker — which is what the previous thread guard was
        # actually relying on anyway.
        _publish_log(r, job_id, "info", "Looking up property and mailing addresses...", db=db)
        try:
            _run_inline_enrichment(db, job, r, job_id, config)
            _publish_log(r, job_id, "success", "Enrichment complete — addresses added", db=db)
        except Exception as exc:
            _logger.warning("Inline enrichment error: %s", str(exc)[:200])
            _publish_log(
                r, job_id, "warning",
                "Address enrichment failed — leads delivered without enriched fields",
                db=db,
            )

        # NTS Tier 1: attach matched trustee-sale auction data onto a pre_foreclosure
        # job's leads (any county with an NTS source — Pierce/Snohomish, King later).
        # Runs HERE (before the post-enrichment refetch below) so the refetched rows +
        # the re-export CSV carry the auction fields; writing after the refetch would
        # leave the just-built CSV stale (Codex). match_job_inline derives the job's
        # county and matches same-county notices. The daily beat re-matches too.
        # Non-fatal — must not fail a delivered job.
        if config.record_type == "pre_foreclosure":
            try:
                from src.workers.nts_matcher_task import (
                    NTS_MATCH_COUNTIES,
                    match_job_inline,
                )
                if (config.county or "").strip().lower() in NTS_MATCH_COUNTIES:
                    n = match_job_inline(db, job_id)
                    if n:
                        _logger.info("Job %s: NTS auction data matched onto %d leads", job_id, n)
            except Exception as exc:
                db.rollback()
                _logger.warning("Job %s: NTS inline match failed: %s", job_id, str(exc)[:120])

        # Fetch post-enrichment rows ONCE; reused by re-export AND membership.
        # Same deterministic order as the in-app download (jobs.py) so the emailed/
        # R2 CSV and the download are byte-identical, not just same-columns (Codex).
        try:
            refreshed = db.execute(
                select(Result)
                .where(Result.job_id == job_id, Result.user_id == job.user_id)
                .order_by(Result.party_name, Result.date_recorded, Result.id)
            ).scalars().all()
        except Exception as exc:
            db.rollback()
            _logger.warning("Job %s: post-enrichment refetch failed: %s", job_id, str(exc)[:120])
            # Sentinel: None means the refetch FAILED. We skip re-export AND
            # membership so we neither overwrite the good export with an empty
            # file nor write partial membership (Codex review).
            refreshed = None

        # Tier 0 (057): authoritative owner-location recompute. This is the single
        # choke point — `refreshed` holds every row AFTER enrichment +
        # _reuse_enrichment_for_duplicates have settled the property/mailing
        # addresses (skip trace runs later and never touches addresses), so one
        # pass here keeps absentee/out_of_state fresh for rows whose mailing was
        # NULL at insert. Non-fatal: a failure must not fail a delivered job.
        if refreshed is not None:
            try:
                _owner_changed = 0
                for res in refreshed:
                    flags = compute_owner_flags(res.property_address, res.mailing_address)
                    if (
                        res.property_state != flags["property_state"]
                        or res.owner_state != flags["owner_state"]
                        or res.absentee_owner != flags["absentee_owner"]
                        or res.out_of_state_owner != flags["out_of_state_owner"]
                    ):
                        res.property_state = flags["property_state"]
                        res.owner_state = flags["owner_state"]
                        res.absentee_owner = flags["absentee_owner"]
                        res.out_of_state_owner = flags["out_of_state_owner"]
                        _owner_changed += 1
                if _owner_changed:
                    db.commit()
                    _logger.info("Job %s: owner flags recomputed for %d rows", job_id, _owner_changed)
            except Exception as exc:
                db.rollback()
                _logger.warning("Job %s: owner-flag recompute failed: %s", job_id, str(exc)[:120])

        # Re-export CSV with enriched data — only if the refetch succeeded.
        if refreshed is not None:
            enriched_file = None
            try:
                record_dicts = [
                    {c: getattr(res, c) for c in [
                        "date_recorded", "party_name", "heirs", "parcel_id",
                        "property_address", "mailing_address", "legal_description",
                        "doc_type",
                        # Structured tax fields (King tax_delinquent; null elsewhere).
                        "delinquent_amount", "delinquent_bill_year",
                        # Owner-location flags (057) so the emailed/R2 CSV carries
                        # absentee/out_of_state/owner_state too (canonical builder reads these).
                        "absentee_owner", "out_of_state_owner", "owner_state",
                        # NTS auction data (059) so the emailed/R2 CSV carries it too.
                        "auction_date", "default_amount",
                        # enrichment_data drives the passthrough cols + derived signals.
                        "enrichment_data", "date_recorded_parsed",
                        # Sprint 4: skip trace fields (may be null on first export
                        # if dispatcher hasn't submitted or webhook hasn't fired).
                        "phone", "phone_type", "email", "skip_trace_status",
                        # Multi-contact arrays so the scheduled/emailed export gets
                        # phone_2/3 + email_2/3 too (canonical builder reads these),
                        # matching the in-app download exactly.
                        "phones", "emails",
                    ]}
                    for res in refreshed
                ]
                enriched_file = exporter.export(
                    record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt,
                    hidden_fields=resolve_hidden_output_fields(config.fields),
                )
                if object_key:
                    exporter.upload_to_r2(enriched_file, object_key)
                    _logger.info("Re-exported CSV with enriched data")
            except Exception as exc:
                _logger.warning("CSV re-export failed: %s", str(exc)[:60])
            finally:
                if enriched_file:
                    enriched_file.unlink(missing_ok=True)

        # ── PHASE 3: RESULT.property_key (combine/overlap join key) ──────────
        # Stamp the strong-identity key on this job's rows BEFORE the membership
        # upsert (Codex order): if membership ever succeeded while this failed,
        # 3B would see overlap with no joinable result rows. Both are isolated
        # and never fail a delivered job; the backfill script heals any gap.
        if refreshed:
            try:
                _pk_updated, _pk_weak = _write_result_property_keys(
                    db, refreshed, str(job.user_id), config.county, config.state
                )
                _logger.info(
                    "Job %s: property_key stamped on %d rows (%d weak-identity skipped)",
                    job_id, _pk_updated, _pk_weak,
                )
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                _logger.error(
                    "Job %s: property_key write FAILED (heal via backfill): %s",
                    job_id, str(exc)[:200],
                )

        # ── PHASE 1: PROPERTY MEMBERSHIP (cross-list overlap rollup) ─────────
        # Strong-identity rollup keyed (user_id, record_type, property_key),
        # computed AFTER enrichment so a probate owner resolved to a parcel
        # overlaps a pre-foreclosure record on the same parcel. Reuses the
        # `refreshed` post-enrichment rows fetched above. Additive + isolated
        # from the billing/dedup path. Durable-with-retry: on hard failure we
        # roll back the poisoned transaction, log, and let
        # scripts/backfill_property_membership.py heal the gap rather than fail
        # an already-delivered job (which would re-email).
        if refreshed:
            try:
                _mcount = _upsert_property_membership(
                    db, refreshed, str(job.user_id), config.record_type,
                    config.county, config.state,
                )
                _logger.info("Job %s: property membership upserted %d properties", job_id, _mcount)
            except Exception as exc:
                # Clear any failed-transaction state so the subsequent
                # _set_status(... "done") write can still succeed (Codex review:
                # the helper only rolls back OperationalError, not e.g.
                # ProgrammingError/IntegrityError/RLS errors).
                try:
                    db.rollback()
                except Exception:
                    pass
                _logger.error(
                    "Job %s: property membership upsert FAILED (heal via backfill): %s",
                    job_id, str(exc)[:200],
                )

        # ── NOW mark done (after enrichment + re-export) ────────────────────
        # record_count reflects unique (non-duplicate) leads — what the user
        # actually sees on the results page. The raw scrape total is in the
        # log: "{N} records saved ({unique} new leads, {dup} duplicates)".
        display_count = max(0, len(records) - dup_count)
        if not _set_status(
            db, job, "done",
            finished_at=_now(),
            record_count=display_count,
            export_key=object_key,
        ):
            # Cancelled (force-finalize) while enriching: the CAS kept the row
            # terminal — suppress the success log, email, and webhook.
            _logger.info(
                "Job %s externally terminalized (%s) — suppressing completion delivery",
                job_id, job.status,
            )
            return
        _publish_log(r, job_id, "success", f"Job complete — {display_count} new leads ({dup_count} duplicates filtered)", db=db)
        r.publish(f"job_logs:{job_id}", json.dumps({"type": "done", "record_count": display_count}))

        # ── IN-APP NOTIFICATION (best-effort; gated by CAS already confirmed above) ──
        from src.workers.notification_emit import create_notification
        create_notification(
            user_id=job.user_id, type="job_completed", job_id=job_id,
            detail={
                "scraper_name": config.name,
                "county": config.county,
                "record_count": display_count,
            },
        )

        # ── EMAIL DELIVERY ─────────────────────────────────────────────────────
        # Build the tokenized 48h download link here (it needs the worker's
        # exporter + API_BASE_URL), then enqueue the send on Celery so a transient
        # Resend blip is RETRIED off the scrape task instead of dropped on the
        # first failure (Fix 3). Building the URL can still raise in prod when
        # API_BASE_URL is unset — that's a delivery-config failure, kept non-fatal
        # for the (already-done) scrape job and surfaced to ops.
        emails = deliver_config.get("emails", [])
        if emails and object_key:
            try:
                download_url = _delivery_download_url(job_id, job.user_id, object_key, exporter)
                deliver_job_email.delay(
                    job_id=job_id,
                    scraper_name=config.name,
                    record_count=display_count,
                    download_url=download_url,
                    recipient_emails=emails,
                    fmt=fmt,
                )
            except Exception as email_exc:
                # Most common cause: API_BASE_URL unset in prod (the URL builder
                # raises). Was silently swallowed — now surfaced to ops so a
                # configured-but-undelivered email is never invisible.
                _logger.warning("Email delivery enqueue failed (non-fatal): %s", email_exc)
                _publish_log(r, job_id, "warning", "Email delivery unavailable", db=db)
                from src.workers.ops_alerts import send_ops_alert
                send_ops_alert(
                    "email_enqueue", job_id,
                    "Lead email could not be queued",
                    f"Could not queue the delivery email for job {job_id}: "
                    f"{str(email_exc)[:200]}",
                )

        # ── SPRINT 6.5: WEBHOOK DELIVERY ───────────────────────────────────────
        # Business+ plan feature (gated at scraper config creation time).
        # Fire-and-forget via Celery so retries happen on the celery queue
        # independently of the scrape job. Non-fatal: webhook failures
        # must never mark the scrape job as errored.
        from src.config.constants import BUSINESS_FEATURES_PLANS
        webhook_url = deliver_config.get("webhook_url")
        _wh_plan_ok = (user.plan or "starter").lower() in BUSINESS_FEATURES_PLANS
        if webhook_url and object_key and not _wh_plan_ok:
            _publish_log(r, job_id, "warning",
                         "Webhook delivery skipped — requires Business plan", db=db)
        if webhook_url and object_key and _wh_plan_ok:
            try:
                from src.workers.webhook_delivery import (
                    build_webhook_payload,
                    deliver_job_webhook,
                )
                signed_download = _delivery_download_url(job_id, job.user_id, object_key, exporter)
                webhook_secret = deliver_config.get("webhook_secret")
                payload = build_webhook_payload(
                    job_id=job_id,
                    scraper_config_id=str(config.id),
                    scraper_name=config.name,
                    county=config.county,
                    state=config.state,
                    record_type=config.record_type,
                    status="done",
                    record_count=len(records),
                    started_at=job.started_at,
                    finished_at=_now(),
                    export_key=object_key,
                    fmt=fmt,
                    download_url=signed_download,
                    webhook_secret=webhook_secret,
                )
                deliver_job_webhook.delay(job_id, webhook_url, payload)
                # Host-only — a webhook URL can carry secrets in its path/query,
                # and this log line is surfaced to the user's job log (Codex).
                from urllib.parse import urlparse
                _wh_host = urlparse(webhook_url).hostname or "the configured endpoint"
                _publish_log(
                    r, job_id, "info",
                    f"Webhook queued for delivery to {_wh_host}",
                    db=db,
                )
            except Exception as webhook_exc:
                _logger.warning(
                    "Webhook enqueue failed (non-fatal) for job %s: %s",
                    job_id, str(webhook_exc)[:200],
                )
                _publish_log(
                    r, job_id, "warning",
                    "Webhook queue unavailable — job completed successfully",
                    db=db,
                )
                from src.workers.ops_alerts import send_ops_alert
                send_ops_alert(
                    "webhook_enqueue", job_id,
                    "Webhook could not be queued",
                    f"Could not queue the completion webhook for job {job_id}: "
                    f"{str(webhook_exc)[:200]}",
                )

        # ── PHASE 5: DIALER PUSH ──────────────────────────────────────────────
        # NOT triggered here. Skip-trace is async (cache-miss rows are filled in
        # later by the Tracerfy webhook), so a push at scrape completion would
        # miss exactly the leads we want (Codex). The dialer push runs in
        # workers/scheduler.dialer_push_sweep once a job's skip-trace has SETTLED.
