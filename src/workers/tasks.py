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

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    address_actionable_sql,
    is_actionable,
)
from src.api.quota_window import (
    window_cte_sql,
    window_set_sql,
)
from src.config.constants import (
    SCRAPE_TRANSIENT_BACKOFF_SECONDS,
    SCRAPE_TRANSIENT_MAX_RETRIES,
    scrape_queue_for_plan,
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
    enrichment_completion_log,
)
from src.workers.tasks_helpers.finalize import (  # noqa: F401  (re-export)
    FinalizeKind,
    _alert_dedup_release_failed,
    _release_claims_of_cancelled_job,
    finalize_billing_and_done,
    release_run_claims_if_owned,
)
from src.workers.tasks_helpers.status import (
    _DELIVERY_TOKEN_TTL,  # noqa: F401  (re-export)
    _TERMINAL_STATUSES,
    HeartbeatThread,
    JobUpdateFields,  # noqa: F401  (re-export)
    _attempt_clauses,
    _delivery_download_url,
    _fail_job,
    _now,
    _publish_log,
    _redis,
    _retry_scrape_job,
    _set_progress,
    _set_stage,
    _set_status,
    attempt_state,
    claim_attempt,
    transient_retry_notice,
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


# Columns pulled off a persisted Result row to build a deliverable export dict. The
# canonical exporter reads typed columns (auction_date/default_amount/...) AND
# enrichment_data; this is the single source of truth shared by the post-enrichment
# re-export and the trustee_sale first-deliverable build (whose auction data lives
# ONLY on the finalized DB rows, never on the in-memory ScrapedRecord).
_RESULT_EXPORT_COLUMNS: tuple[str, ...] = (
    "date_recorded", "party_name", "heirs", "parcel_id",
    "property_address", "mailing_address", "legal_description", "doc_type",
    "delinquent_amount", "delinquent_bill_year",
    "absentee_owner", "out_of_state_owner", "owner_state",
    "auction_date", "default_amount",
    "enrichment_data", "date_recorded_parsed",
    "phone", "phone_type", "email", "skip_trace_status",
    "phones", "emails",
    # Stored structured situs (migration 085). Missing here, the scheduled file fell
    # back to parsing a street-only property_address and blanked the city/state/zip
    # the in-app download showed for the same rows.
    "property_city", "property_state", "property_zip",
)


def _result_rows_to_export_dicts(rows) -> list[dict]:
    """Project persisted Result rows onto the exporter's expected dict shape."""
    return [{c: getattr(res, c) for c in _RESULT_EXPORT_COLUMNS} for res in rows]


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


def account_charge_state(db, user_id, *, lock: str):
    """(block, now): `block` is 'frozen' or 'ended' when this account may no
    longer be charged for records, else None (audit #4 S4-01); `now` is the
    database clock the decision was made at. The same rule the skip-trace claim
    applies (`paid_lookup_access`), so the two cannot disagree.

    The users row is read under `lock`, and `now` is read AFTER the lock is held:
    waiting for a billing write can carry the run past a grace deadline, a term
    end or a quota window end, and a clock read before the wait would still judge
    the moment the wait began (Codex 4a review round 2; audit S4-06). A caller
    that charges under the same lock judges its window at this `now` too, so the
    access decision and the charge cannot straddle a boundary."""
    from src.workers.skip_trace_claim import (
        ACCESS_ENDED,
        ACCESS_FROZEN,
        paid_lookup_access,
        read_access_rows,
    )

    account = read_access_rows(db, [user_id], lock=lock).get(str(user_id))
    now = db.execute(sa_text("SELECT clock_timestamp()")).scalar()
    if account is None:
        return ACCESS_ENDED, now
    access = paid_lookup_access(account, now)
    return (access if access in (ACCESS_FROZEN, ACCESS_ENDED) else None), now


def account_charge_block(db, user_id, *, lock: str) -> str | None:
    """'frozen' or 'ended' when this account may no longer be charged for records,
    else None. See `account_charge_state`."""
    return account_charge_state(db, user_id, lock=lock)[0]


def reserve_job_quota(db, *, job_id: str, user_id, want: int) -> int | None:
    """RESERVE this job's share of the account's record quota, do not merely read it.

    Returns the records granted, or None when this job already holds a
    reservation (a watchdog re-run: the caller reuses ``jobs.reserved_count``).
    Does NOT commit: the caller owns the transaction.

    Reading remaining quota and only charging it later, after the export, left
    the allowance unguarded in between: two concurrent jobs (or two children of
    one batch) both read the same remaining N and both delivered N. The atomic
    increment at billing made the totals SUM correctly, which faithfully records
    the over-delivery rather than preventing it. A lock cannot span the gap
    either, because the caller commits before the export runs.

    LOCK ORDER: jobs, then users — the same order billing and
    release_quota_reservation use. Locking users first (the obvious way to write
    this) inverts against them and lets a watchdog re-run deadlock with an
    attempt already in billing: one holds users and wants jobs, the other holds
    jobs and wants users. (Codex)

    THE CLOCK IS READ ONCE THE USERS ROW IS HELD (audit S4-06). The grant
    evaluates the entitlement window at that moment. A clock read before the lock
    judged a reservation that waited across ``quota_period_end`` (another job
    reserving, billing settling) against the window that had already ended: the
    old counter, the old limit, and the old window recorded on the job.
    """
    # 1. CAS-claim on the JOB. Only the attempt that flips reserved_at from NULL
    #    reserves, so a watchdog re-run of this same job reuses its grant instead
    #    of taking a second one. The value written here is only the claim marker;
    #    step 3 replaces it with the post-lock clock the grant was judged at.
    claimed = db.execute(
        sa_text(
            "UPDATE jobs SET reserved_at = clock_timestamp() "
            "WHERE id = :jid AND reserved_at IS NULL"
        ),
        {"jid": job_id},
    ).rowcount
    if not claimed:
        return None

    # 1b. The account may still be charged (S4-01, Codex 4a review). The
    #    pre-scrape check is a read, and a freeze or a term end can commit while
    #    the scrape runs. Decided HERE under the users row lock the grant below
    #    takes anyway (jobs, then users: the order above), so nothing can land
    #    between this decision and the grant. A frozen or ended account is
    #    granted nothing: every row is capped, so nothing is delivered, billed or
    #    traced. `reserved_at` is the clock read under that lock: the decision,
    #    the window and the job's record all use this one instant.
    block, reserved_at = account_charge_state(db, user_id, lock="FOR UPDATE")
    if block:
        _logger.warning(
            "Job %s: account %s froze or ended during the run; granting 0 records",
            job_id, user_id,
        )
        want = 0
    # 2. Compute the grant and consume it in ONE statement. FOR UPDATE serialises
    #    concurrent reservations for this user: the loser blocks, then re-reads
    #    the already-decremented remainder (READ COMMITTED re-evaluates a locked
    #    row against the newer version) and is granted only what is truly left.
    #    LAZY ROLLOVER lives in this same statement. The entitlement window is
    #    advanced, the counter zeroed and any pending downgrade applied atomically
    #    with the grant, so a boundary crossed between two concurrent reservations
    #    cannot be seen half-applied — and the grant is computed against the
    #    POST-rollover limit, so a boundary cannot leak one job's worth of the
    #    outgoing cap into the new window. (window_cte_sql / WINDOW_SET_SQL in
    #    src/api/quota_window.py — the ONE definition.)
    res_row = db.execute(
        sa_text(
            "WITH cur AS ("
            "  SELECT u.id, u.records_used, u.records_limit,"
            "         u.quota_anchor_at, u.quota_period_start,"
            "         u.quota_period_end, u.subscription_status,"
            "         u.entitlement_grace_ends_at, u.entitlement_ends_at,"
            "         u.pending_plan, u.pending_records_limit"
            "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
            "), w AS ("
            "  SELECT cur.*, " + window_cte_sql() + " FROM cur"
            "), g AS ("
            "  SELECT w.*,"
            "         LEAST(:want, GREATEST(0, eff_limit - base))"
            "           AS granted"
            "  FROM w"
            ") UPDATE users u SET"
            "    records_used = g.base + g.granted,"
            + window_set_sql("g")
            + "  FROM g WHERE u.id = g.id"
            "  RETURNING g.granted, g.new_start"
        ),
        {"want": want, "uid": str(user_id), "at": reserved_at},
    ).one()
    granted = int(res_row.granted or 0)
    # 3. Record what was granted AND which entitlement window it was charged to,
    #    so settlement and release can tell "still current" from "the window this
    #    was charged to has since rolled and been zeroed". Comparing calendar
    #    months (the previous test) is only accidentally right while every window
    #    starts on the 1st. Same job row, already locked by step 1 — no new lock
    #    is taken. `new_start` is never NULL (users.quota_anchor_at,
    #    quota_period_start and quota_period_end are NOT NULL, and
    #    public.quota_next_start is STRICT over them and a non-NULL `at`),
    #    so reservation_is_current_sql never falls back to the pre-088 reading
    #    of `reserved_at`, and replacing the claim marker here changes nothing
    #    for settlement or release.
    db.execute(
        sa_text(
            "UPDATE jobs SET reserved_count = :n, "
            "quota_period_start = CAST(:ws AS timestamptz), "
            "reserved_at = CAST(:at AS timestamptz) "
            "WHERE id = :jid"
        ),
        {"n": granted, "jid": job_id, "ws": res_row.new_start, "at": reserved_at},
    )
    return granted


def skip_reason_for_config(active: bool, paused_reason: str | None) -> str | None:
    """Why a job for this scraper must not run, in the user's words, or None.

    A deleted scraper is active=False; a plan downgrade pause is active=False
    with paused_reason='entitlement'. The pause is checked on its own, so a row
    that contradicts itself (active=True, reason still set) is not run either."""
    from src.api.entitlements import PAUSED_REASON_ENTITLEMENT

    if paused_reason == PAUSED_REASON_ENTITLEMENT:
        return "This scraper is paused on your current plan, so this run was skipped."
    if not active:
        return "This scraper was deleted, so this run was skipped."
    return None


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
                    *_attempt_clauses(expected_started_at),
                    Job.status.notin_(_TERMINAL_STATUSES),
                    Job.billing_applied_at.is_(None),
                )
                .values(status="failed", finished_at=_now(), error_message=reason)
                .returning(Job.user_id)
            ).fetchone()
            db.commit()
            if row is not None:
                # The crash may have happened after the plan cap RESERVED quota
                # but before billing settled it. That reservation is already
                # charged to the user, so without this it becomes a permanent
                # charge for records they never received. The CAS above already
                # required billing_applied_at IS NULL, so this cannot refund a
                # job that legitimately billed.
                from src.workers.tasks_helpers.status import (
                    release_quota_reservation,
                )
                release_quota_reservation(db, job_id)
                # The crash may ALSO have happened after the dedup step claimed
                # hashes in delivered_records (it commits at the dedup step, long
                # before delivery) but before anything was delivered. Releasing
                # the quota and keeping the claims is the worst of both: the user
                # is not charged, gets no leads, and every FUTURE run silently
                # drops those same leads as "already delivered" — permanently
                # unreachable. Every other failure path in this file already
                # releases; this one released quota only (2026-09-08 audit).
                #
                # first_job_id scopes the delete to claims THIS job made, so a
                # hash an earlier run legitimately owns is untouched.
                try:
                    db.execute(
                        sa_text(
                            "DELETE FROM delivered_records "
                            "WHERE first_job_id = :jid AND user_id = CAST(:uid AS uuid)"
                        ),
                        {"jid": job_id, "uid": str(row.user_id)},
                    )
                    db.commit()
                except Exception as _rel_exc:  # noqa: BLE001
                    db.rollback()
                    _alert_dedup_release_failed(
                        job_id, row.user_id, "post_crash_cleanup", _rel_exc
                    )
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
                "Job failed during processing. Our team has been notified.",
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
    from sqlalchemy import func, select

    from src.api.middleware.security import register_connector_domains_from_db
    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import rls_sync_session, system_sync_session
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class
    from src.utils.data_exporter import DataExporter
    from src.utils.lead_export import (
        resolve_export_layout,
        resolve_hidden_output_fields,
    )
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
        # claim_job_for_attempt also stamps last_heartbeat_at with the same instant
        # as started_at, so every attempt begins with a FRESH liveness observation
        # and can never be re-queued on the previous attempt's stale one. See that
        # helper for why the CAS lives there rather than inline here.
        attempt_token = claim_attempt(db, job_id)
        if attempt_token is None:
            _logger.info(
                "Job %s not claimable (already in flight / not pending) — "
                "skipping to avoid double-scrape",
                job_id,
            )
            return
        db.refresh(job)

        def _still_ours(landed: bool) -> bool:
            """After a stage write: carry on, or stop this attempt.

            Landed -> carry on. Not landed means the attempt token changed, the job went
            terminal, or the write hit a swallowed telemetry error (_set_progress never
            raises); the row decides which, under its lock. Only the last one carries
            on. A stopped attempt publishes and commits nothing more (Codex 2c-bis
            diff r4: a stale attempt used to keep narrating onto the replacement's log
            and, at `connecting`, commit its own date window onto that row).
            """
            if landed:
                return True
            owned = attempt_state(db, job_id, _boot_user_id, attempt_token).owned
            db.rollback()
            if not owned:
                _logger.info(
                    "Job %s: the attempt token changed or the job is terminal; "
                    "this attempt stops", job_id,
                )
            return owned

        # THIS attempt's token, held in a local rather than read off the ORM object
        # each time. Every progress and stage write below is scoped to it, so a
        # callback that arrives late — from an attempt the watchdog already replaced
        # — updates nothing instead of overwriting the live attempt's observations.
        # A local cannot drift: `job.started_at` is re-read by every db.refresh()
        # and would silently start naming whichever attempt owns the row now.
        # Record THIS attempt's started_at on the Celery request so the on_failure hook
        # (_RunScrapeJobTask) can attempt-scope its crash cleanup — it must only fail the
        # row if started_at still matches, never a re-queued/re-claimed newer attempt.
        try:
            self.request.scrape_started_at = attempt_token
        except Exception:  # request context unavailable (e.g. direct call) — non-fatal
            pass

        # A deleted or plan-paused scraper never runs, whoever queued the job.
        # A delete or pause can land after the Job was created: a batch fan-out
        # between its read and its commit, or any job queued earlier (UX audit
        # F-043, Codex P1). This is the one check every path passes through.
        # Re-read under this attempt's claim so the row is current. Not gated on
        # ENTITLEMENT_ENFORCEMENT: this is the user's own delete, or a pause
        # that already happened, not an entitlement decision.
        db.refresh(config)
        _skip_reason = skip_reason_for_config(config.active, config.paused_reason)
        if _skip_reason is not None:
            # _fail_job writes the job log line, emits the event and releases any
            # reserved quota, so a skipped run reserves and bills nothing.
            _fail_job(db, job, r, job_id, _skip_reason, expected_started_at=attempt_token)
            return

        # The ACCOUNT may still start billable work (audit #4 S4-01). The enqueue
        # gates ask run_eligibility, but a job can sit queued, be re-run by the
        # watchdog or fan out from a batch after its account froze for
        # non-payment or its paid term ended. Re-read under this attempt's claim.
        # over_limit is deliberately NOT re-checked: this job's own reservation
        # counts toward usage, so a watchdog re-run would refuse itself, and the
        # reservation below already grants only what is left.
        from src.api.quota import run_eligibility

        db.refresh(user)
        _eligibility = run_eligibility(user)
        if _eligibility.code in ("frozen", "ended"):
            _fail_job(db, job, r, job_id, _eligibility.message,
                      expected_started_at=attempt_token)
            return

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
            # _violation is an entitlements.Violation; str() is its customer-facing
            # message. Both strings below reach the user (live log + job error).
            # _fail_job publishes the same line, and only once its CAS lands.
            _fail_job(db, job, r, job_id, f"{_violation.title}. {_violation.message}",
                      expected_started_at=attempt_token)
            return

        # Liveness heartbeat RE-ENABLED (2026-09-09) under the condition the
        # 2026-06-18 rollback set: the heartbeat now runs on `heartbeat_engine`, a
        # DEDICATED NullPool engine (src/db/session.py), so it can never contend
        # with the pool_size=2 work pool that the main session and _publish_log
        # share. That contention is what deadlocked every scrape at the insert
        # phase and got this disabled.
        #
        # It was disabled for long enough that last_heartbeat_at was NULL on every
        # job in production, which quietly made the watchdog's 15-minute
        # stale-heartbeat branch DEAD CODE: a job whose worker was killed mid-scrape
        # (a deploy, an OOM, a hard timeout) sat visibly "running" for the full
        # 70-minute started_at fallback with nothing to show it was gone. That is
        # exactly what stranded job 9c8b7259 on 2026-09-09.
        _hb.start(attempt_token)
        # Stage rides the commit that _publish_log already performs, so no new commit
        # point is introduced into the work session (see _set_progress).
        if not _still_ours(_set_stage(db, job, "preparing", expected_started_at=attempt_token,
                                      commit=False)):
            return
        _publish_log(r, job_id, "info", f"Job queued: {config.name} ({config.county}, {config.state})", db=db)

        # ── PROBING ───────────────────────────────────────────────────────────
        if not _set_status(db, job, "probing", expected_started_at=attempt_token):
            _logger.info("Job %s externally terminalized (%s) — aborting", job_id, job.status)
            return
        _publish_log(r, job_id, "info", "Probing county portal...", db=db)

        try:
            scraper_class, matched_record_type = get_scraper_class(config.county, config.state, config.record_type)
        except UnsupportedCountyError as exc:
            reason = str(exc)
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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
        if not _set_status(db, job, "scraping", expected_started_at=attempt_token):
            _logger.info("Job %s externally terminalized (%s) — aborting", job_id, job.status)
            return
        record_label = config.record_type.replace("_", " ").title()
        _publish_log(r, job_id, "success", f"Starting scrape: {record_label} records", db=db)

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
        _trim_notice = None
        if max_days:
            from datetime import timedelta as _td
            _df = datetime.strptime(date_from, "%m/%d/%Y")
            _dt = datetime.strptime(date_to, "%m/%d/%Y")
            actual_days = (_dt - _df).days
            if actual_days > max_days:
                # Trim date_from to respect the limit (keep the most recent data)
                _df = _dt - _td(days=max_days)
                date_from = _df.strftime("%m/%d/%Y")
                _trim_notice = (
                    f"{config.county.title()} County supports max {max_days} days. "
                    f"Range trimmed to {date_from} → {date_to}."
                )

        # The resolved window is written ONLY onto this attempt's row: a token-scoped
        # UPDATE, not an ORM assignment flushed by primary key, which a stale attempt
        # would have committed onto the replacement's row (Codex 2c-bis diff r5). The
        # ORM copy is updated without being marked dirty, so no later flush re-writes
        # it unfenced. Nothing is published until the write has landed.
        from sqlalchemy import update as _sa_update
        from sqlalchemy.orm.attributes import set_committed_value

        _dated = db.execute(
            _sa_update(Job)
            .where(Job.id == job_id, Job.status.not_in(_TERMINAL_STATUSES),
                   *_attempt_clauses(attempt_token))
            .values(date_from=date_from, date_to=date_to)
        ).rowcount
        if not _still_ours(_dated == 1):
            return
        set_committed_value(job, "date_from", date_from)
        set_committed_value(job, "date_to", date_to)
        if _trim_notice:
            _publish_log(r, job_id, "warning", _trim_notice, db=db)
        _publish_log(r, job_id, "info", f"Date range: {date_from} → {date_to} (mode: {range_mode})", db=db)

        _last_phase = [None]  # mutable for closure
        # The stage the worker last WROTE. The scraper reports a phase on every
        # progress callback, but re-writing the same stage each time would restart
        # its clock, and the "still working on this" copy is driven by that clock
        # (Codex). So the stage moves only when the phase actually changes.
        _last_stage = [None]
        # Scraper phase -> customer-facing stage. A connector reports "searching"
        # once, then starts collecting; without this the label stayed on
        # "Searching county records" for the entire scrape.
        _PHASE_STAGES = {
            "scraping": "scraping",
            "parcel_lookup": "enriching",
            "enriching": "enriching",
        }

        def _on_progress(
            page_current, page_total, record_count=None, phase="scraping", unit=None,
        ):
            """Called by the scraper as it works — updates the DB in real time.

            Writes BOTH the legacy NOT NULL counters (page_current/page_total/
            record_count, kept for every existing reader) and the migration-099
            observations, which is the pair that can tell UNKNOWN from ZERO.

            A ``page_total`` of 0 means "no denominator yet", not "zero pages", so it
            is stored as units_total=NULL. That distinction is the whole reason the
            Live Run page can stop rendering a fabricated 0%. ``records_found`` is
            written straight through, including a real 0, because a county that
            genuinely returned nothing is a legitimate answer and must look different
            from one we have not asked yet.

            ``unit`` names what a unit IS so the UI never calls chunks "pages". A
            scraper that does not say is left NULL rather than defaulted to "page" —
            the counters are still shown, the word for them just is not guessed.
            """
            observations: dict = {
                # The legacy NOT NULL counters, written in the SAME guarded
                # statement as the observations rather than by an ORM assignment
                # committed by primary key. They used to be their own unconditional
                # write, which is the defect BE #347 fixed in the watchdog and
                # missed here: after the watchdog re-queues a stranded attempt, a
                # late callback from the OLD worker would still land these on a row
                # that now belongs to a replacement run — or to a finished one,
                # overwriting the billed record_count the terminal CAS just set
                # (Codex round 4). One write, one precondition, one answer.
                "page_current": page_current,
                "page_total": page_total,
                "units_done": page_current,
                "units_total": page_total if page_total > 0 else None,
                "last_progress_at": _now(),
            }
            if record_count is not None:
                # record_count is NOT NULL: a None would raise. It stays at whatever
                # was last observed, which is the honest reading of "no new count".
                observations["record_count"] = record_count
                observations["records_found"] = record_count
            if unit:
                observations["progress_unit"] = unit
            # A phase change carries the stage with it, in the SAME statement as the
            # counters it belongs to — so the activity and the numbers describing it
            # can never disagree, and the stage clock restarts exactly once per real
            # transition rather than on every callback.
            # phase=None is "counts, no stage claim": _PHASE_STAGES.get(None) is
            # None, so `advancing` stays False and the stage the connector last
            # reported survives. That is what lets a chunked scraper publish its
            # denominator AFTER its startup transitions — which clear the counters
            # — without also asserting that the scrape has begun (Codex round 6).
            stage_for_phase = _PHASE_STAGES.get(phase)
            advancing = bool(stage_for_phase and stage_for_phase != _last_stage[0])
            if advancing:
                observations["stage"] = stage_for_phase
                observations["stage_started_at"] = _now()
            landed = _set_progress(
                db, job, expected_started_at=attempt_token, **observations,
            )
            if landed and advancing:
                # The local mirror advances only when the row did. Moving it first
                # would leave this worker believing it had announced a stage the
                # database refused, and silently skipping the retry.
                _last_stage[0] = stage_for_phase
            if not landed:
                # This attempt no longer owns the row: it was re-queued, cancelled
                # or finished under us. The counters were correctly refused, and the
                # phase log below must be refused with them — otherwise a dead
                # worker still narrates "Looking up addresses for 48 parcels..." onto
                # the replacement run's stream, where it reads as live (Codex round
                # 4). _last_phase is deliberately left alone: there is no later
                # callback from this attempt that could legitimately publish it.
                return

            # Log phase transitions so the frontend shows what's happening
            if phase != _last_phase[0]:
                _last_phase[0] = phase
                if phase == "parcel_lookup":
                    _publish_log(r, job_id, "info", f"Looking up parcel IDs from detail pages ({page_total} records)...", db=db)
                elif phase == "enriching":
                    _publish_log(r, job_id, "info", f"Looking up addresses for {page_total} parcels...", db=db)

        def _on_stage(stage: str) -> None:
            """Called by the scraper when it enters a named activity.

            This is the signal that closes the dead zone. `status` goes to 'scraping'
            and then says nothing more for the whole scrape — 401 seconds on the run
            we traced — because everything from browser launch through captcha to the
            first result page happens inside one call. The scraper knows which of
            those it is in; nothing else does.
            """
            if _set_stage(db, job, stage, expected_started_at=attempt_token):
                _last_stage[0] = stage

        if not _still_ours(_set_stage(
            db, job, "connecting", expected_started_at=attempt_token, commit=False,
        )):
            return
        _publish_log(r, job_id, "info", "Connecting to county portal...", db=db)
        # Flush the resolved date window to disk before entering the scraper, which
        # can run for up to _SCRAPE_TIMEOUT below. Until this commits, job.date_from
        # / job.date_to exist only in this session's uncommitted transaction, so the
        # live page and any support query would show a NULL window for the whole run.
        #
        # This block used to also set `job.progress_label = "Connecting to portal..."`.
        # That was dead: `progress_label` is NOT a column on Job and is in no
        # migration (production confirms `column jobs.progress_label does not exist`)
        # — it is a COMPUTED field on the JobProgress response schema, derived in
        # src/api/schemas.py from status + page counters. The assignment therefore
        # just set a stray Python attribute on the ORM instance and wrote nothing,
        # while the log message below claimed a progress_label write had failed.
        # Removed rather than implemented: schemas.py already derives a better label
        # from state that is actually persisted.
        try:
            db.commit()
        except Exception as commit_exc:
            _logger.warning(
                "Job %s: failed to commit the resolved date window before scraping: %s",
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
                    _run_scraper(scraper_class, date_from, date_to, r, job_id, _on_progress, record_type=matched_record_type, doc_types=config.doc_types, on_stage=_on_stage),
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
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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
        except (SoftTimeLimitExceeded, TimeLimitExceeded):
            # A Celery time limit must ESCAPE this handler, not be classified by it.
            # `_RunScrapeJobTask.on_failure` already treats timeouts as RECOVERABLE
            # and deliberately declines to terminalize them, so the watchdog can
            # re-queue the attempt — but that only works if the exception reaches
            # the task boundary. Caught here it is just another `Exception`:
            # `is_transient_scrape_error` does not know Celery, so it reads as
            # PERMANENT, `_fail_job` runs, the task then returns normally, and
            # `on_failure` never fires. A recoverable timeout became a dead job.
            #
            # True for a timeout landing anywhere in the scrape, which predates this
            # change; re-raising them out of the telemetry writes (so they cannot be
            # swallowed there either) made a second route into the same handler,
            # which is how it was noticed (Codex round 8).
            #
            # No rollback: the work session is held in a `with rls_sync_session(...)`
            # block whose exit closes it and releases the jobs-row lock.
            raise
        except Exception as exc:
            _logger.exception("Scraper error for job %s", job_id)
            # attempt_token is the token the CLAIM returned, captured once at
            # the top of this run and never re-read from the ORM. It attempt-scopes
            # BOTH the retry CAS and the terminal fail below, so a stale/superseded
            # attempt never clobbers a live re-claimed one (Codex P1). This used to
            # re-read `job.started_at` here "before the rollback expires it", which
            # was the right instinct aimed one step short: a refresh that had already
            # happened would hand back the NEWER attempt's value, which is precisely
            # the value that must not be used.
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
                    db, job, job_id, attempt_token,
                    max_retries=SCRAPE_TRANSIENT_MAX_RETRIES,
                    backoffs=SCRAPE_TRANSIENT_BACKOFF_SECONDS,
                )
                if countdown is not None:
                    # BOTH sides of this merge were needed. This branch replaced
                    # the inline PRIORITY_QUEUE_PLANS test with
                    # scrape_queue_for_plan, which normalizes the plan first — an
                    # untrimmed or uppercased plan silently fell to the standard
                    # queue and a paying customer lost priority. main
                    # independently added `published = True`, which the except
                    # below flips to False and the watchdog branch depends on.
                    # Taking either side alone would have dropped the other.
                    queue = scrape_queue_for_plan(user.plan if user else None)
                    published = True
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
                        published = False
                        _logger.warning(
                            "run_scrape_job retry publish failed for job %s; left "
                            "'pending' for watchdog re-delivery", job_id, exc_info=True,
                        )
                    # USER-FACING copy — see transient_retry_notice for the wording
                    # rules. Deliberately separate from the engineering log below,
                    # which is the one allowed to carry the exception class.
                    _publish_log(
                        r, job_id, "warning",
                        transient_retry_notice(
                            retry_count=job.retry_count,
                            max_retries=SCRAPE_TRANSIENT_MAX_RETRIES,
                            countdown=countdown,
                            published=published,
                        ),
                        db=db,
                    )
                    # ENGINEERING log. Carries the exception CLASS as well as its
                    # message: the message alone ("Timeout 15000ms exceeded") does not
                    # say whether this was a Playwright timeout, a TransientScrapeError
                    # or a ScraperBlockedError, and that distinction is the first thing
                    # anyone triaging a stuck job needs.
                    _logger.warning(
                        "Job %s: transient scrape error in the SCRAPE phase — re-queued "
                        "(retry %d/%d, countdown %ds, published=%s): %s: %s",
                        job_id, job.retry_count, SCRAPE_TRANSIENT_MAX_RETRIES,
                        countdown, published, type(exc).__name__, str(exc)[:200],
                    )
                    return
            reason = "Scraper encountered an error. Our team has been notified."
            # Attempt-scoped: only fail the job if THIS attempt still owns it
            # (started_at unchanged). If a newer attempt re-claimed it — or the
            # retry CAS above no-oped on an ownership change — this no-ops instead
            # of terminalizing a live newer attempt (Codex P1).
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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

        # The authoritative raw scrape total, recorded on the row rather than left in
        # the log. Most connectors never report a running count (several call
        # on_progress once, at the very end), and `record_count` cannot be used for
        # this: the done-CAS overwrites it with the BILLED non-duplicate count, so a
        # run that found 57 and billed 2 reads 2 forever after. This is the number in
        # the line below, which is the number the customer was just told.
        # 0 is written as 0 on purpose — a county that returned nothing really did.
        _set_progress(
            db, job,
            expected_started_at=attempt_token,
            commit=False,
            records_found=len(records),
            last_progress_at=_now(),
        )
        _publish_log(r, job_id, "success", f"Scrape complete: {len(records)} records found", db=db)

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

        # ── Plan quota ────────────────────────────────────────────────────────
        # NOT capped here. A row's actionability is unknowable at this point: the
        # counties whose addresses arrive during inline enrichment (King probate,
        # the generic GIS sweep) look addressless until then. Slicing the RAW list
        # to the quota could therefore save a fully-quarantined prefix, bill ~0,
        # and silently discard real leads the user still had quota for. The cap is
        # applied after enrichment instead, against the same actionable set that
        # display, export and billing use — see "APPLY THE PLAN CAP" below
        # (Codex ruling, 2026-09-03).

        # ── ENRICHING ─────────────────────────────────────────────────────────
        # CAS no-op here means a batch force-finalize cancelled this child while
        # it was scraping (>90min stuck): discard the scrape without saving,
        # billing, or delivering — the batch already recorded it as timed out.
        if not _set_status(db, job, "enriching", record_count=len(records),
                           expected_started_at=attempt_token):
            _logger.info(
                "Job %s externally terminalized (%s) mid-scrape — discarding without billing",
                job_id, job.status,
            )
            return
        if not _still_ours(_set_stage(db, job, "saving", expected_started_at=attempt_token,
                                      commit=False)):
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
                # Migration 085: keep the situs city/zip the SOURCE gave us (a notice's
                # "commonly known as" line carries them) — enrichment later replaces
                # property_address with the assessor's street-only line, which would
                # otherwise throw them away. Parsed, never guessed: blank when absent.
                from src.utils.lead_formatting import parse_property_for_display as _ppd
                _situs = _ppd(rec.property_address) if rec.property_address else {}
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
                    "property_city": _trunc(_situs.get("city"), 128),
                    "property_zip": _trunc(_situs.get("zip") or rec.property_zip, 10),
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
        if not _still_ours(_set_stage(db, job, "deduping", expected_started_at=attempt_token,
                                      commit=False)):
            return
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
                #
                # Migration 089: stamp WHICH run holds the claim, NOW, while the
                # answer is still knowable. The results page tells the user these
                # were "already delivered"; without this it could not show them
                # where, and the link it offered instead was chosen by an
                # unrelated rule that pointed at a run two months LATER (see
                # get_results). delivered_records cannot answer it after the fact:
                # claims are released and re-claimed, source jobs are purged, and
                # the request path holds no privilege on that table at all.
                # LEFT JOIN, so a hash whose claim has already gone stamps NULL
                # and is reported as unattributed rather than mis-attributed.
                for j in range(0, len(duplicate_result_ids), 500):
                    chunk = duplicate_result_ids[j:j + 500]
                    db.execute(
                        sa_text(
                            "UPDATE results r SET is_duplicate = true, "
                            "  duplicate_reason = 'prior_run', "
                            "  duplicate_source_job_id = dr.first_job_id, "
                            "  duplicate_source_at = dr.first_delivered_at "
                            "FROM results src "
                            "LEFT JOIN delivered_records dr "
                            "  ON dr.user_id = CAST(:uid AS uuid) "
                            " AND dr.dedup_hash = src.dedup_hash "
                            "WHERE r.id = src.id "
                            "  AND r.user_id = CAST(:uid AS uuid) "
                            "  AND src.id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": chunk, "uid": str(job.user_id)},
                    )
                db.commit()

        _publish_log(
            r, job_id, "success",
            f"{len(records)} records saved ({unique_count} new leads, {dup_count} duplicates)",
            db=db,
        )

        # ── AUCTION LEADS (trustee_sale) FINALIZE ───────────────────────────────
        # An Auction Lead IS a known Notice-of-Trustee-Sale row; the scraper stamped
        # its id + auction fields into enrichment_data["nts_source"]. Populate the
        # typed Result auction columns DIRECTLY from that (no fuzzy matching, unlike
        # pre_foreclosure's nts_matcher). Runs HERE — before billing below — so a
        # broken finalize fails the job WITHOUT charging, and before the post-
        # enrichment re-export so the delivered CSV carries auction data. FAIL-CLOSED
        # (Codex): never deliver an Auction Lead with blank auction/default/trustee
        # data. On failure, release this job's dedup claims (committed at the dedup
        # step above) and fail loudly — mirrors the R2-upload-failure handler below.
        if config.record_type == "trustee_sale":
            from src.workers.trustee_sale_finalize import finalize_trustee_sale_job
            try:
                # Fold the same-parcel collapse into dup_count so the user-facing
                # record_count / completion log / notification / email reflect it
                # (billing reads a fresh DB non-dup count and is already correct;
                # display_count = len(records) - dup_count was not) (Codex).
                dup_count += finalize_trustee_sale_job(db, job_id, job.user_id)
            except Exception as exc:
                _logger.error(
                    "Job %s: trustee_sale finalize FAILED — failing job (no blank "
                    "auction leads): %s", job_id, str(exc)[:200],
                )
                try:
                    db.rollback()
                    release_run_claims_if_owned(db, job_id, _boot_user_id, attempt_token)
                    db.commit()
                except Exception as cleanup_exc:
                    db.rollback()
                    _logger.error(
                        "Job %s: failed to release dedup claims after finalize "
                        "failure: %s", job_id, str(cleanup_exc)[:200],
                    )
                    _alert_dedup_release_failed(job_id, _boot_user_id, "finalize_failure", cleanup_exc)
                reason = (
                    "Auction data could not be attached to your Auction Leads, so the "
                    "run was stopped and you were not charged. Please try again; "
                    "contact support if it keeps failing."
                )
                if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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
            # Every OTHER record type collapses its same-run siblings here.
            # dedup_hash is the app-wide BILLING key, but the cross-job scan only
            # records that a hash was CLAIMED once — it leaves same-JOB rows
            # sharing a hash all is_duplicate=false, and billing counts ROWS. So
            # a run that scraped two filings on one property charged for both.
            #
            # trustee_sale has collapsed its own siblings since 2026-07-03 (with
            # an auction-aware survivor rule, hence the branch). Nothing else
            # did: an audit on 2026-09-08 found 8 completed probate and
            # pre_foreclosure jobs that had charged 50 records for properties
            # already billed in the same run, including a 122-record job that
            # covered 120 properties. Runs BEFORE billing, in the same
            # transaction, so the charge reflects the collapse.
            from src.workers.tasks_helpers.dedup import collapse_same_run_siblings
            _collapsed = collapse_same_run_siblings(
                db, job_id, job.user_id, config.record_type
            )
            if _collapsed:
                dup_count += _collapsed
                _publish_log(
                    r, job_id, "info",
                    f"Combined {_collapsed} record(s) already covered by another "
                    "record in this run — you are charged once per property.",
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

        # Export runs BEFORE enrichment, which is why the stage list is not a
        # pipeline and why nothing may read "step N of M" off it.
        if not _still_ours(_set_stage(db, job, "exporting", expected_started_at=attempt_token,
                                      commit=False)):
            return
        _publish_log(r, job_id, "info", f"Building {fmt.upper()} export...", db=db)

        # Build the FIRST deliverable from the PERSISTED rows for every record type.
        # trustee_sale always needed this (auction data lives only on the typed DB
        # columns — the in-memory ScrapedRecords carry nts_source instead), and the
        # standing "never deliver a duplicate" rule below needs it for the rest: the
        # in-memory records carry no `is_duplicate`, because that flag is written by
        # the dedup pass a few hundred lines above, against the DB. Reading the DB
        # here also makes this export and the post-enrichment re-export agree by
        # construction instead of by coincidence. Mailing is NULL pre-enrichment; the
        # later re-export refreshes it.
        _export_rows = db.execute(
            select(Result)
            .where(Result.job_id == job_id, Result.user_id == job.user_id)
            .order_by(Result.party_name, Result.date_recorded, Result.id)
        ).scalars().all()
        # Standing rule (owner, 2026-09-02): rows with no property AND no mailing
        # address are not leads — never in the deliverable. They stay in `results`
        # (dedup/health). Addresses filled by enrichment surface in the re-export.
        #
        # Standing rule (owner, 2026-09-04): a DUPLICATE is never delivered either.
        # It was already delivered — and paid for — on an earlier run, so shipping it
        # again put rows in the CSV that the completion email, the webhook and the
        # bill all reported as zero. The row stays in `results` as dedup bookkeeping;
        # it just never reaches a deliverable. Lists/segments and the batch combined
        # export deliberately KEEP duplicates (a lead whose only contactable row is a
        # duplicate must not vanish there) — this rule is per-job delivery only.
        #
        # Both filters run on the ORM rows, BEFORE projection: is_duplicate is not one
        # of _RESULT_EXPORT_COLUMNS, so filtering the projected dicts would silently
        # never match. Mirrors the re-export below.
        record_dicts = _result_rows_to_export_dicts(
            [res for res in _export_rows if is_actionable(res) and not res.is_duplicate]
        )
        # Honor the user's output-field visibility (blank deselected hideable
        # columns; identity/derived columns always present). Legacy/empty => all.
        hidden_fields = resolve_hidden_output_fields(config.fields)
        # Lean per-record-type columns: a single scrape job is ONE record type, so
        # the emitted file drops columns that type can never populate (probate ships
        # no tax/code-violation/auction columns). Same subset for the enriched
        # re-export below, keeping the R2 file and the in-app download identical. The
        # batch COMBINED export is a separate superset path (batch_export.py).
        # Same layout + source context as the in-app download (jobs.py), so the
        # delivered file and the downloaded file have identical headers and values.
        export_layout = (
            config.deliver.get("csv_layout") if isinstance(config.deliver, dict) else None
        )
        export_columns, export_labels = resolve_export_layout(export_layout, config.record_type)
        export_context = {
            "county": config.county, "state": config.state, "record_type": config.record_type,
        }
        exporter = DataExporter()
        local_file = exporter.export(
            record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt,
            hidden_fields=hidden_fields, columns=export_columns,
            labels=export_labels, context=export_context,
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
                release_run_claims_if_owned(db, job_id, _boot_user_id, attempt_token)
                db.commit()
            except Exception as cleanup_exc:
                db.rollback()
                _logger.error(
                    "Job %s: failed to release dedup claims after upload failure: %s",
                    job_id, str(cleanup_exc)[:200],
                )
                _alert_dedup_release_failed(job_id, _boot_user_id, "upload_failure", cleanup_exc)
            # Honest message: a FAILED job is terminal — the watchdog does NOT
            # re-queue it (it only requeues stuck active/pending jobs). A
            # scheduled scraper makes a fresh job on its next occurrence; a manual
            # run must be re-triggered by the user. Don't promise auto-retry.
            reason = (
                "Export upload to cloud storage failed after multiple attempts. "
                "No file was produced and you were not charged. Please run the "
                "scraper again; contact support if it keeps failing."
            )
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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
        if not _still_ours(_set_stage(db, job, "enriching", expected_started_at=attempt_token,
                                      commit=False)):
            return
        _publish_log(r, job_id, "info", "Looking up property and mailing addresses...", db=db)
        # Skip trace is enqueued only after a completed enrichment, and only after
        # the plan cap below (never for a row that will not be delivered).
        _enrichment_ok = False
        try:
            # `enrich_summary` lets this line tell the truth. It used to announce
            # "Enrichment complete" unconditionally, so a job that looked up 0 of
            # 153 mailing addresses still reported success, two lines under its own
            # warning. A partially enriched job is not a failed job, but it is not
            # a complete one either, and the user needs to be able to tell the
            # difference between "scrape failed" and "some enrichment is pending".
            enrich_summary: dict = {}
            _run_inline_enrichment(db, job, r, job_id, config, summary=enrich_summary)
            _enrichment_ok = True
            _level, _msg = enrichment_completion_log(enrich_summary)
            _publish_log(r, job_id, _level, _msg, db=db)
        except Exception as exc:
            _logger.warning("Inline enrichment error: %s", str(exc)[:200])
            _publish_log(
                r, job_id, "warning",
                "Address enrichment failed. Leads were delivered without enriched fields.",
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

        # Re-elect each same-run group's survivor now that enrichment has
        # settled. collapse_same_run_siblings had to run before the export and
        # the billing count, so it ranked on the addresses as SCRAPED — and the
        # Pierce legal-description repair inside _run_inline_enrichment can fill
        # an address onto the row that lost, leaving the survivor undeliverable
        # and the one actionable row flagged is_duplicate with no retry able to
        # revisit it (Codex P2).
        #
        # HERE specifically: after enrichment and the NTS match, and BEFORE the
        # refetch below — the re-export and the property membership both read
        # from that refetch, and the plan cap ranks non-duplicates, so a swap
        # after either would be invisible to the CSV or rank the wrong row.
        # Nothing else reads is_duplicate between the two points.
        #
        # Elects exactly one survivor per group, so the duplicate count and the
        # charge cannot move. Non-fatal: a delivered job must not fail here.
        try:
            from src.workers.tasks_helpers.dedup import (
                reconcile_same_run_survivors,
            )
            _swapped = reconcile_same_run_survivors(
                db, job_id, job.user_id, config.record_type
            )
            # Commit unconditionally: the pass also merges the losers' source-only
            # fields onto each survivor, and those writes happen even when no
            # survivor actually moved. Gating the commit on _swapped would leave
            # them riding on someone else's transaction, to be lost by the next
            # rollback.
            db.commit()
            if _swapped:
                _logger.info(
                    "Job %s: re-elected %d same-run survivor(s) after enrichment",
                    job_id, _swapped,
                )
        except Exception as exc:
            db.rollback()
            _logger.warning(
                "Job %s: same-run reconciliation failed: %s", job_id, str(exc)[:160]
            )

        # Take over any claim whose holder never delivered the lead: an earlier
        # run's address-less row pins a property that THIS run found with an
        # address, and without this the lead is hidden as "already delivered"
        # forever. HERE: actionability is settled (after enrichment and the
        # re-election) and the refetch below still precedes the plan cap, skip
        # trace, the re-export and billing, so all four see the promoted row.
        # Each claim commits on its own. Non-fatal: a failure leaves the rows
        # flagged exactly as the cross-run dedup left them.
        try:
            from src.workers.tasks_helpers.dedup import transfer_undelivered_claims

            _transferred = transfer_undelivered_claims(
                db, job_id, job.user_id, config.record_type
            )
            if _transferred:
                dup_count -= _transferred
                _logger.info(
                    "Job %s: took over %d claim(s) an earlier run held without "
                    "ever delivering the lead", job_id, _transferred,
                )
        except Exception as exc:
            db.rollback()
            _logger.warning(
                "Job %s: claim transfer failed: %s", job_id, str(exc)[:160]
            )

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
                    flags = compute_owner_flags(
                        res.property_address, res.mailing_address,
                        property_city=res.property_city, property_state=res.property_state,
                        property_zip=res.property_zip,
                    )
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

        # ── APPLY THE PLAN CAP (after enrichment, before delivery) ───────────
        # Every scraped row was persisted and enriched, so actionability is now
        # known. Mark everything past the user's remaining quota as excluded:
        # lead_actionability then hides those rows from display, export, counting
        # AND billing at once, so the four can never disagree.
        #
        # A finite-quota user whose refetch FAILED cannot be capped safely (we
        # cannot tell which rows are actionable), and delivering an uncapped
        # export would over-deliver. Fail before billing rather than guess —
        # same rule as the enriched re-export below.
        _capped_ids: list[str] = []
        # EFFECTIVE limit, not the stored one: an Agency subscriber with a
        # pending downgrade whose window has ended would otherwise skip the cap
        # block entirely (records_limit == -1), export uncapped, and only then
        # have settlement roll the window and apply the smaller limit — landing
        # them at 5000/1000. (Codex)
        from src.api.quota import effective_records_limit as _eff_limit

        # An account that froze or ended during the run enters the cap block
        # whatever its limit (S4-01, Codex 4a review round 2): an unlimited plan
        # would otherwise skip it and deliver. Inside, the decision is re-made
        # under the users row lock and grants 0 records. This first read is only
        # the gate into the block; for an unlimited account that stays live it is
        # the whole check, so a freeze committed after it and before delivery
        # (the export that follows) is not caught here.
        _charge_block = account_charge_block(db, job.user_id, lock="")
        if _eff_limit(user) != -1 or _charge_block:
            _cap_error: Exception | None = None
            if refreshed is None:
                _cap_error = RuntimeError("post-enrichment refetch failed")
            else:
                try:
                    # RESERVE the quota before capping (reserve_job_quota: lock order,
                    # window rollover and the watchdog re-run CAS are documented there).
                    _want = db.execute(
                        sa_text(
                            "SELECT count(*) FROM results "
                            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
                            f"  AND is_duplicate = false AND {address_actionable_sql('results')}"
                        ),
                        {"jid": job_id, "uid": str(job.user_id)},
                    ).scalar() or 0

                    _remaining = reserve_job_quota(
                        db, job_id=job_id, user_id=job.user_id, want=_want,
                    )
                    if _remaining is None:
                        # Already reserved (watchdog re-run): reuse the grant.
                        db.refresh(job)
                        _remaining = int(job.reserved_count or 0)
                        _logger.info(
                            "Job %s: quota already reserved (%d) — reusing",
                            job_id, _remaining,
                        )
                    # Clear this job's previous marks first: on a watchdog re-run
                    # the ranking must start from the FULL actionable set, or each
                    # pass would renumber the survivors and mark a second batch,
                    # shrinking the delivered set every time.
                    db.execute(
                        sa_text("UPDATE results SET enrichment_data = (CASE WHEN jsonb_typeof(COALESCE(enrichment_data, '{}')::jsonb) = 'object' THEN COALESCE(enrichment_data, '{}')::jsonb ELSE '{}'::jsonb END - :key)::json WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) AND enrichment_data->>:key = :reason"),
                        {"jid": job_id, "uid": str(job.user_id),
                         "key": DELIVERY_EXCLUDED_KEY, "reason": OVER_QUOTA},
                    )
                    # Tax delinquent ranks largest balance first and code violations
                    # open-then-newest (plan_cap.py); others keep party_name, date, id.
                    from src.workers.tasks_helpers.plan_cap import mark_over_quota_rows

                    _capped_ids = mark_over_quota_rows(
                        db, job_id=job_id, user_id=str(job.user_id),
                        remaining=_remaining, record_type=config.record_type,
                    )
                    if _capped_ids:
                        # A capped row's SAME-RUN siblings must inherit the
                        # exclusion (Codex P1). The cap ranks non-duplicates only,
                        # so a sibling collapsed by collapse_same_run_siblings is
                        # invisible here — and lists and the batch combined export
                        # deliberately KEEP duplicates. Without this, a property
                        # whose survivor was excluded for quota would still be
                        # delivered through its sibling, unpaid, while the claim
                        # release below frees the hash for yet another charge.
                        db.execute(
                            sa_text(
                                "UPDATE results sib SET enrichment_data = "
                                "  (CASE WHEN jsonb_typeof(COALESCE(sib.enrichment_data, '{}')::jsonb) = 'object' "
                                "        THEN COALESCE(sib.enrichment_data, '{}')::jsonb "
                                "        ELSE '{}'::jsonb END "
                                "   || jsonb_build_object(:key, :reason))::json "
                                "FROM results capped "
                                "WHERE capped.id = ANY(CAST(:ids AS uuid[])) "
                                "  AND capped.user_id = CAST(:uid AS uuid) "
                                "  AND sib.job_id = :jid "
                                "  AND sib.user_id = CAST(:uid AS uuid) "
                                "  AND sib.dedup_hash = capped.dedup_hash "
                                "  AND sib.dedup_hash IS NOT NULL "
                                "  AND sib.duplicate_reason = 'same_run'"
                            ),
                            {"ids": _capped_ids, "uid": str(job.user_id),
                             "jid": job_id, "key": DELIVERY_EXCLUDED_KEY,
                             "reason": OVER_QUOTA},
                        )
                        # Release the dedup claims of rows we are NOT delivering,
                        # so a later run (or next month's quota) can still deliver
                        # them. Keeping the claim would make the lead permanently
                        # unreachable — the same invariant the re-export failure
                        # path protects.
                        # Release what the cap excluded, KEEPING any claim a
                        # surviving deliverable sibling in this job still needs.
                        # The statement lives in tasks_helpers/dedup.py so the
                        # tests can run the real one instead of a copy (Codex).
                        from src.workers.tasks_helpers.dedup import (
                            release_capped_dedup_claims,
                        )
                        # Only while this attempt still owns the job: the claims are
                        # keyed by job, which a replacement attempt shares. A lost
                        # attempt stops here through the cap's own error path.
                        if not attempt_state(db, job_id, _boot_user_id, attempt_token).owned:
                            raise RuntimeError("attempt token changed during the plan cap")
                        release_capped_dedup_claims(
                            db, str(job.user_id), job_id, _capped_ids
                        )
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    _cap_error = exc

            if _cap_error is not None:
                _logger.error(
                    "Job %s: plan cap failed — failing job before billing: %s",
                    job_id, str(_cap_error)[:200],
                )
                try:
                    db.rollback()
                    release_run_claims_if_owned(db, job_id, _boot_user_id, attempt_token)
                    db.commit()
                except Exception as cleanup_exc:
                    db.rollback()
                    _logger.error(
                        "Job %s: failed to release dedup claims after cap failure: %s",
                        job_id, str(cleanup_exc)[:200],
                    )
                    _alert_dedup_release_failed(job_id, _boot_user_id, "plan_cap_failure", cleanup_exc)
                reason = (
                    'The lead list could not be re-read after enrichment, so your plan quota could not be applied. No file was delivered and you were not charged. Please run the scraper again; contact support if it keeps failing.' if refreshed is None else 'Your plan quota could not be applied to this run. No file was delivered and you were not charged. Please run the scraper again; contact support if it keeps failing.'
                )
                if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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

            if _capped_ids:
                _publish_log(
                    r, job_id, "warning",
                    f"Plan limit: delivering {_remaining} of "
                    f"{_remaining + len(_capped_ids)} leads. Upgrade for more.",
                    db=db,
                )
                _logger.info(
                    "Job %s: plan cap excluded %d actionable rows (remaining=%d)",
                    job_id, len(_capped_ids), _remaining,
                )

        # ── SKIP TRACE ENQUEUE (once delivery is decided) ────────────────────
        # Only here, after the survivor re-election and the plan cap, which are
        # the last steps that decide which rows ship. It used to run at the end
        # of inline enrichment, before both: a lookup could be bought for a row
        # the re-election then demoted or the cap then excluded, while the row
        # actually delivered was never traced. The enqueue selects non-duplicate,
        # actionable (so not over-quota), not-yet-attempted rows, so reading the
        # settled flags is all the fix needs.
        #
        # Non-fatal, exactly as it was inside enrichment: everything before this
        # point has already committed, so the rollback can only discard the
        # enqueue's own uncommitted work, and an unqueued lead stays
        # 'not_attempted' for a later backfill rather than failing a delivery.
        #
        # Only when enrichment succeeded (main #280). Commit first so the rollback
        # can only ever discard the enqueue's own writes, never earlier job work;
        # a savepoint would not help, the enqueue commits internally.
        if _enrichment_ok:
            db.commit()
            # ENQUEUE only. The provider answers minutes or hours later, through a
            # webhook, long after this job is terminal — so the stage is named for
            # what the job is actually doing ("queuing contact lookups"), not for the
            # skip trace itself. A run that shows "finding contact information" and
            # then finishes would be claiming work it never waited for.
            # Passed as a callback rather than written here: the stage is only true
            # once the enqueue's own gates have passed (skip trace enabled, token
            # present, config on, plan above Starter, at least one eligible row).
            # Written unconditionally, it labelled a Starter run — or any run with
            # nothing eligible — "Queuing contact lookups" while queuing nothing
            # (Codex round 7). Restating those gates here would just give them a
            # second place to drift.
            try:
                _enqueue_skip_trace_rows(
                    db, job, r, job_id, config,
                    on_begin=lambda: _set_stage(
                        db, job, "queuing_contacts",
                        expected_started_at=attempt_token,
                    ),
                )
            except Exception as exc:
                db.rollback()
                _logger.warning(
                    "Job %s: skip trace enqueue failed: %s", job_id, str(exc)[:160]
                )

        # Reload the rows every consumer below reads, for EVERY plan.
        #
        # populate_existing is load-bearing, not defensive: the sessions are
        # built with expire_on_commit=False (src/db/session.py), and these
        # Result identities were already loaded by the post-enrichment refetch
        # above. A plain re-SELECT returns those SAME objects with their STALE
        # state, so the export would miss what raw SQL wrote since: the plan
        # cap's over-quota marker (the export would ship rows billing then
        # refuses to charge, Codex 2026-09-03), a row the claim transfer
        # promoted, and the contact data a skip-trace cache hit just copied in.
        # Billing reads the DB, so a stale export is a file/bill disagreement.
        if refreshed is not None:
            refreshed = db.execute(
                select(Result)
                .where(Result.job_id == job_id, Result.user_id == job.user_id)
                .order_by(Result.party_name, Result.date_recorded, Result.id)
                .execution_options(populate_existing=True)
            ).scalars().all()

        # Re-export CSV with enriched data. A refetch that FAILED is a re-export
        # failure too, for every plan: the R2 object is still the pre-enrichment
        # file, while billing below counts the rows as they are now, including any
        # that enrichment made actionable or the claim transfer promoted. Finite
        # plans already failed on this before the cap; unlimited plans used to skip
        # the re-export silently and bill a count the file did not match (Codex).
        reexport_error: Exception | None = None
        if refreshed is None:
            reexport_error = RuntimeError("post-enrichment refetch failed")
        else:
            enriched_file = None
            try:
                # Deliverable = actionable, NON-DUPLICATE rows (see the first export
                # above for both rules); `refreshed` itself stays complete for the
                # property_list_membership upsert, which is cross-job overlap evidence
                # and must still see duplicates.
                record_dicts = _result_rows_to_export_dicts(
                    [res for res in refreshed
                     if is_actionable(res) and not res.is_duplicate]
                )
                enriched_file = exporter.export(
                    record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt,
                    hidden_fields=resolve_hidden_output_fields(config.fields),
                    columns=export_columns,
                    labels=export_labels, context=export_context,
                )
                if object_key:
                    upload_ok, upload_exc = _upload_export_with_retry(
                        exporter, enriched_file, object_key
                    )
                    if upload_ok:
                        _logger.info("Re-exported CSV with enriched data")
                    else:
                        reexport_error = upload_exc
            except Exception as exc:
                reexport_error = exc
            finally:
                if enriched_file:
                    enriched_file.unlink(missing_ok=True)
        if reexport_error is not None:
            # The R2 object is the PRE-enrichment deliverable, which (by the
            # actionability rule) omits rows that enrichment has since made
            # actionable — and those rows are about to be billed. A bill that
            # does not match the delivered file is never acceptable, so this is
            # fatal exactly like the first upload: release the dedup claims,
            # fail before billing, and let the user re-run (Codex).
            _logger.error(
                "Job %s: enriched re-export failed — failing job before billing: %s",
                job_id, str(reexport_error)[:200],
            )
            try:
                db.rollback()
                release_run_claims_if_owned(db, job_id, _boot_user_id, attempt_token)
                db.commit()
            except Exception as cleanup_exc:
                db.rollback()
                _logger.error(
                    "Job %s: failed to release dedup claims after re-export failure: %s",
                    job_id, str(cleanup_exc)[:200],
                )
                _alert_dedup_release_failed(job_id, _boot_user_id, "reexport_failure", cleanup_exc)
            reason = (
                "The lead file could not be refreshed with enriched addresses. "
                "No file was delivered and you were not charged. Please run the "
                "scraper again; contact support if it keeps failing."
            )
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
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

        # Billing and the done transition, in one transaction: moved verbatim to
        # src/workers/tasks_helpers/finalize.py so the tests run production code.
        _outcome = finalize_billing_and_done(
            db, r, job=job, user=user, config=config, job_id=job_id,
            attempt_token=attempt_token, object_key=object_key,
            boot_user_id=_boot_user_id,
        )
        if _outcome.kind is not FinalizeKind.DONE:
            return
        display_count = _outcome.display_count

        if user.records_limit != -1 and user.records_used > user.records_limit:
            overage = user.records_used - user.records_limit
            _publish_log(r, job_id, "warning", f"Plan limit exceeded by {overage} records. Upgrade to keep scraping.", db=db)
        _publish_log(r, job_id, "success", f"Job complete: {display_count} new leads ({dup_count} duplicates filtered)", db=db)
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
                         "Webhook delivery skipped. Webhooks require the Business plan.", db=db)
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
                    # Same deliverable count as the job/email/UI (non-duplicate,
                    # actionable) — len(records) included duplicates and
                    # unactionable rows (Codex).
                    record_count=display_count,
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
                    "Webhook queue unavailable. Your job completed successfully.",
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
