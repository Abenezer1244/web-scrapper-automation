"""Skip-trace dispatcher (Sprint 4, Celery Beat task).

Runs every 5 minutes. Drains the `pending_skip_trace_rows` table in
FIFO order, groups rows by `trace_type` (normal / advanced), and submits
up to `SKIP_TRACE_MAX_BATCHES_PER_TICK` batches to Tracerfy per tick.

Why a dispatcher instead of per-job calls:
- Tracerfy's batch POST endpoint is rate-limited to 10 requests per
  5-minute window per account. If N parallel scrape jobs each submit
  their own batch at the nightly scheduler fire time, we trip 429s.
- The dispatcher consolidates all jobs' rows into 1-2 POSTs per tick,
  which fits comfortably under the rate limit.
- Each batch can contain thousands of rows, so throughput is fine —
  the constraint is burst count, not total volume.

This module does NOT ingest webhook results — that's handled by
`src/api/routes/webhooks.py::tracerfy_webhook`. The dispatcher's only
job is submission and queue-record bookkeeping.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from src.config import settings
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.skip_trace_capacity import (
    BATCH_ROW_LIMIT,
    allocate,
    batch_rows,
    credits_for,
    lock_allocated,
    resolve_caps,
    row_allowance,
    spent_credits,
    take_within_caps,
)

_logger = setup_logger("worker.skip_trace_dispatcher")

# The rolling window both spend caps are measured over.
_SPEND_WINDOW = timedelta(days=1)
# Bounds on one pass's refill (Codex ii-b consult T1, U2): round r allocates up to
# 2**r times each account's remaining room, so a run of k blocked rows is passed in
# about log2(k) rounds. Past either bound the pass claims what it has found.
_REFILL_MAX_ROUNDS = 12
_REFILL_DEADLINE = timedelta(seconds=2)


@app.task(name="src.workers.skip_trace_dispatcher.dispatch_pending_skip_trace")
def dispatch_pending_skip_trace() -> dict:
    """Drain pending_skip_trace_rows and submit batches to Tracerfy.

    Returns a small dict summarizing the tick's activity (for log dumping
    and Flower inspection): {submitted_batches, submitted_rows, errors}.
    """
    if not settings.SKIP_TRACE_ENABLED:
        _logger.debug("SKIP_TRACE_ENABLED=False — dispatcher tick skipped")
        return {"skipped": "disabled"}

    if not settings.TRACERFY_API_TOKEN:
        _logger.warning("TRACERFY_API_TOKEN missing — dispatcher tick skipped")
        return {"skipped": "no_token"}

    from sqlalchemy import and_, func, select, text, update
    from sqlalchemy import true as sa_true

    from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
    from src.api.results_category import skip_trace_eligible_condition
    from src.db.models import Job, PendingSkipTraceRow, Result
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import TracerfyError, submit_batch
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

    # SPEND CIRCUIT BREAKER. Every Tracerfy lookup costs the operator real money,
    # and until now the only ceiling was the prepaid balance returning 402 — i.e.
    # the spend limit was "until the money runs out". This trips BEFORE any claim
    # or provider call.
    #
    # Placed here, at the top of the tick beside SKIP_TRACE_ENABLED, ON PURPOSE:
    # it only ever STOPS work and never touches the queued -> submitting state
    # machine below. That machine has careful unknown-outcome handling (a
    # mis-step double-pays or strands paid rows), and this path already stalled
    # every tenant for 7+ hours once. A breaker that can only decline to start a
    # tick cannot cause that class of failure.
    #
    # Unset or 0 = DISABLED. Rows are not lost when it trips: they stay queued
    # and flow on a later tick under the cap. This is only the cheap early exit
    # (and the page) for the GLOBAL cap; the caps themselves are enforced inside
    # the claim lock below, per pass, where they are hard.
    #
    # NOTE: do NOT import datetime/timedelta inside this function. They are
    # imported at module scope and used later in it; a function-local
    # `from datetime import ...` rebinds the name as LOCAL for the whole body, so
    # every later `datetime.now(UTC)` raises UnboundLocalError on the path where
    # the import did not run. That once broke dispatch outright.
    caps = resolve_caps()
    if caps.global_cap:
        from src.db.session import system_sync_session as _sess

        with _sess() as _db:
            spent_now, _ = spent_credits(_db, datetime.now(UTC) - _SPEND_WINDOW)
        if spent_now >= caps.global_cap:
            _logger.error(
                "Skip-trace DAILY CAP reached: %d credits claimed in the last 24h "
                "(cap %d, from %s). Holding this tick; queued rows are untouched and "
                "resume when the rolling window clears or the cap is raised.",
                spent_now, caps.global_cap, caps.global_source,
            )
            try:
                from src.workers.ops_alerts import send_ops_alert

                send_ops_alert(
                    "skip_trace_daily_cap", "dispatcher",
                    "Skip-trace daily spend cap reached",
                    f"{spent_now} Tracerfy credits were claimed in the last 24h, at or "
                    f"above {caps.global_source}={caps.global_cap} (a normal lookup costs "
                    f"1 credit, an advanced one 2). Dispatch is paused. Raise the cap or "
                    f"investigate whether an account is driving unexpected volume.",
                )
            except Exception:  # noqa: BLE001 — an alert failure must not change the decision
                _logger.exception("skip-trace daily-cap alert failed to send")
            return {"skipped": "daily_cap", "spent_credits": spent_now,
                    "cap_credits": caps.global_cap, "cap_source": caps.global_source}

    max_batches = max(1, settings.SKIP_TRACE_MAX_BATCHES_PER_TICK)
    submitted_batches = 0
    submitted_rows = 0
    errors: list[str] = []

    # SYSTEM SESSION: the dispatcher drains pending rows across all
    # tenants in a single pass (Tracerfy batches are grouped by
    # trace_type, not by user). This is a legitimate cross-tenant
    # system operation.
    with system_sync_session() as db:
        # DEFENCE IN DEPTH FOR THE DOUBLE CHARGE (Security Master Review pass 10).
        #
        # Migration 100's unique index is what stops one lead holding two active
        # claims, and the claim path refuses to write without it. But this
        # dispatcher is what SPENDS money, and it drains rows that already exist.
        # If 100 ever aborts -- which happens precisely when duplicates are
        # already present -- start.sh still boots the worker, and the dispatcher
        # would submit both rows of a duplicate pair and charge the customer
        # twice for one lead.
        #
        # So when the invariant is unenforced, say so loudly and skip exactly the
        # leads that are duplicated, rather than halting every tenant's lookups
        # over a condition most of them are not in. When it IS enforced, which is
        # the normal case, this costs one catalog read and nothing else.
        _duplicated_result_ids: set[str] = set()
        from src.workers.skip_trace_claim import warn_if_unenforced

        if not warn_if_unenforced(db):
            _duplicated_result_ids = {
                str(r) for r in db.execute(text(
                    "SELECT result_id FROM pending_skip_trace_rows "
                    "WHERE status IN ('queued','submitting','submitted') "
                    "GROUP BY result_id HAVING count(*) > 1"
                )).scalars()
            }
            if _duplicated_result_ids:
                _logger.error(
                    "Dispatcher: %d lead(s) hold more than one ACTIVE pending row "
                    "while migration 100 is not enforced. They are NOT being "
                    "submitted, because doing so would charge for the same lead "
                    "twice. Reconcile them and apply 100.",
                    len(_duplicated_result_ids),
                )
                try:
                    from src.workers.ops_alerts import send_ops_alert

                    send_ops_alert(
                        "skip_trace_duplicate_active_rows", "dispatcher",
                        "Duplicate active skip-trace rows while 100 is unenforced",
                        f"{len(_duplicated_result_ids)} lead(s) hold more than one "
                        f"active pending row and migration 100's unique index is "
                        f"missing or invalid. Those leads are held back rather than "
                        f"submitted, so nothing is double charged, but they will not "
                        f"be looked up until this is reconciled and 100 applied.",
                    )
                except Exception:  # noqa: BLE001 - an alert must not stop the tick
                    _logger.exception("skip-trace duplicate-rows alert failed to send")

        # Resolve anything stuck mid-submission from a previous tick BEFORE
        # draining new work: a released claim rejoins the FIFO head below and
        # goes out in this same tick instead of waiting another five minutes.
        reconciled = _reconcile_stale_claims(db)
        # Never pay for a lead that will not be delivered: cancel queued rows whose
        # job failed or was cancelled, or whose lead is over quota, a same-run sibling,
        # or no longer waiting on a trace.
        # The sweep no longer commits or swallows its own failure (15-5): the
        # transaction boundary lives here, at the caller, which is what lets
        # Phase 1b-2 add the action-disposition and audit writes to this SAME
        # transaction. `swept is None` keeps its existing meaning of "the sweep
        # failed", which the compliance gate immediately below depends on.
        swept: int | None
        try:
            swept = _cancel_undeliverable_queued(db)
            db.commit()
            if swept:
                _logger.info("Dispatcher: cancelled %d undeliverable queued row(s)", swept)
        except Exception as exc:  # noqa: BLE001 - never break the submit loop
            db.rollback()
            _logger.warning("Dispatcher: cancel sweep failed: %s", str(exc)[:160])
            swept = None
        # The sweep is best-effort for the deliverability rules (a failed tick retries in
        # five minutes), but it is also where a Tacoma row enqueued before the paid switch
        # was turned off is withdrawn. While that switch is off, a failed sweep must stop
        # the tick rather than fall through to the submit loop (Codex P1). The loop's own
        # query excludes those rows too; this is the second lock on the same door.
        if swept is None and not settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED:
            _logger.error("Dispatcher: compliance sweep failed; skipping this tick so no "
                          "ATIP-named Tacoma row can be submitted")
            return _tick_result(0, 0, ["compliance sweep failed"], deferred="sweep_failed")
        # Settle what this account already has an answer for (a twin's lookup landed, a
        # fresh cache entry) before anything is bought. Best-effort: on failure the rows
        # stay queued, and the in-flight hold below still stops a second purchase.
        _settle_queued_from_known_answers(db)

        for _ in range(max_batches):
            # Pick a trace_type to drain this pass. Prefer 'normal' first
            # (cheaper per row), then 'advanced'. Within a pass we batch
            # one trace_type only because Tracerfy takes trace_type as a
            # top-level field on the POST.
            for trace_type in ("normal", "advanced"):
                # One tick at a time from here to the claim commit, which releases it
                # (transaction-scoped). Without it, two overlapping ticks could each see
                # the other's twin as not yet claimed and buy one answer twice.
                if not db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY}
                ).scalar():
                    db.rollback()
                    _logger.info("Dispatcher: another tick is claiming; deferring to the next tick")
                    return _tick_result(submitted_batches, submitted_rows, errors,
                                        deferred="claim_locked")
                # Everything from here to the claim commit runs under that lock, in
                # READ COMMITTED: every earlier claim committed its submitted_at before
                # releasing it, so the spend read below includes it, and no two
                # passes can claim the same allowance (Codex ii-b consult U1).
                cost = credits_for(trace_type)
                caps = resolve_caps()
                global_spent, spent_by_user = spent_credits(
                    db, datetime.now(UTC) - _SPEND_WINDOW)
                global_rows = batch_rows(caps.global_cap, global_spent, cost)
                default_rows = row_allowance(caps.account_cap, 0, cost)
                # Each account's room for this pass, for the accounts that already
                # spent; any other account has default_rows (None = no account cap).
                account_room = (
                    None if default_rows is None else
                    {u: row_allowance(caps.account_cap, s, cost)
                     for u, s in spent_by_user.items()}
                )
                if global_rows == 0:
                    db.rollback()
                    _logger.info(
                        "Dispatcher: no %s rows fit under the global cap (%d of %d "
                        "credits claimed in 24h)", trace_type, global_spent, caps.global_cap,
                    )
                    continue
                # Best-effort bound on rows arriving mid-pass (V1). The hard bound on
                # the pass is the round limit and deadline below.
                watermark = db.execute(text("SELECT clock_timestamp()")).scalar()

                def _eligible(exclude: set[str], trace_type=trace_type, watermark=watermark):
                    """Every row that may be bought now, as (id, user_id, enqueued_at).
                    Eligibility is decided HERE, in SQL, not after a LIMIT: filtering in
                    Python would let ineligible rows occupy the head and starve
                    everything behind them. A row goes out only once its job delivered
                    (done, or billed whatever status was written over it, or a terminal
                    job from before billing was stamped: _job_delivered_sql) and its
                    lead is still deliverable and still waiting. Tenant-pinned joins:
                    this runs in a system session."""
                    return (
                        select(
                            PendingSkipTraceRow.id,
                            PendingSkipTraceRow.user_id,
                            PendingSkipTraceRow.enqueued_at,
                        )
                        .join(
                            Job,
                            and_(
                                Job.id == PendingSkipTraceRow.job_id,
                                Job.user_id == PendingSkipTraceRow.user_id,
                            ),
                        )
                        .join(
                            Result,
                            and_(
                                Result.id == PendingSkipTraceRow.result_id,
                                Result.user_id == PendingSkipTraceRow.user_id,
                            ),
                        )
                        .where(
                            and_(
                                PendingSkipTraceRow.status == "queued",
                                PendingSkipTraceRow.trace_type == trace_type,
                                PendingSkipTraceRow.enqueued_at <= watermark,
                                # Already considered this pass: ranked out, so the
                                # next round ranks what is LEFT (S1).
                                PendingSkipTraceRow.id.notin_(exclude)
                                if exclude else sa_true(),
                                # Empty in the normal case (100 enforced), so this
                                # is a no-op unless the invariant is actually off.
                                PendingSkipTraceRow.result_id.notin_(
                                    _duplicated_result_ids
                                ) if _duplicated_result_ids else sa_true(),
                                text(_job_delivered_sql("jobs")).bindparams(
                                    since=BILLING_STAMP_RELIABLE_SINCE),
                                Result.skip_trace_status == "queued",
                                # Delivered now, or by an earlier run of this account:
                                # same-run siblings and superseded rows are never bought.
                                skip_trace_eligible_condition(),
                                func.coalesce(
                                    Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
                                ) != OVER_QUOTA,
                                # An ATIP-named Tacoma lead is never submitted while the
                                # paid switch is off, whatever the sweep above did.
                                _atip_paid_allowed_sql(),
                            )
                        )
                        .subquery("eligible")
                    )

                # REFILL (Codex R2, R3, S1, T1, U2). Rounds allocate fairly ranked
                # candidates, lock them (SKIP LOCKED: another tick's rows are passed
                # over), filter them, and hold duplicates CUMULATIVELY over everything
                # found so far. Only rows that survive all of that are charged to an
                # account's room or the batch, so a blocked head is replaced by the
                # next row instead of costing its account a turn.
                rows: list = []
                withdrawn: list = []
                unsubmittable: list = []
                considered: set[str] = set()
                left_queued = held = 0
                started = datetime.now(UTC)
                truncated = None
                rounds = 0
                while True:
                    if rounds >= _REFILL_MAX_ROUNDS:
                        truncated = "round_limit"
                        break
                    # Never before the first round: a pass always gets one allocation.
                    if rounds and datetime.now(UTC) - started >= _REFILL_DEADLINE:
                        truncated = "deadline"
                        break
                    global_left = global_rows - len(rows)
                    if global_left <= 0:
                        break
                    look = 2 ** rounds
                    room_left = None
                    if account_room is not None:
                        taken_by_user: dict[str, int] = {}
                        for r in rows:
                            u = str(r.user_id)
                            taken_by_user[u] = taken_by_user.get(u, 0) + 1
                        room_left = {
                            u: max(0, account_room.get(u, default_rows) - taken_by_user.get(u, 0))
                            for u in set(account_room) | set(taken_by_user)
                        }
                    ids = allocate(
                        db, _eligible(considered),
                        account_rows=room_left or {}, default_rows=default_rows,
                        lookahead=look, limit=min(BATCH_ROW_LIMIT, global_left * look),
                    )
                    rounds += 1
                    if not ids:
                        break
                    considered.update(ids)
                    new = lock_allocated(db, ids)
                    # Buy a lookup only for a lead that was actually delivered.
                    new, drop, later = _partition_still_deliverable(db, new)
                    if drop:
                        _cancel_undeliverable(db, drop)
                        withdrawn.extend(drop)
                    left_queued += later
                    new, bad = _partition_submittable(new)
                    if bad:
                        _fail_unsubmittable(db, bad)
                        unsubmittable.extend(bad)
                    # One answer, bought once: no second row for an (account, address)
                    # whose lookup is at Tracerfy now or earlier in this batch. Earlier
                    # survivors come first, so they keep their place (R2).
                    kept, n_held = _hold_answers_in_flight(db, rows + new)
                    held += n_held
                    rows = take_within_caps(
                        kept, account_room=account_room, default_rows=default_rows,
                        global_rows=global_rows,
                    )
                if truncated:
                    _logger.warning(
                        "Dispatcher: refill_truncated reason=%s trace_type=%s rounds=%d "
                        "elapsed_ms=%d global_rows=%d global_left=%d considered=%d "
                        "blocked=%d survivors=%d",
                        truncated, trace_type, rounds,
                        (datetime.now(UTC) - started) / timedelta(milliseconds=1),
                        global_rows, global_rows - len(rows), len(considered),
                        len(considered) - len(rows), len(rows),
                    )
                if withdrawn:
                    _logger.info(
                        "Dispatcher: %d %s row(s) withdrawn before submit: the job did "
                        "not deliver, or the lead is now a same-run sibling or over the plan "
                        "limit", len(withdrawn), trace_type,
                    )
                if left_queued:
                    _logger.info(
                        "Dispatcher: %d %s row(s) left queued for a later tick (job "
                        "still running, or the lead is being updated)",
                        left_queued, trace_type,
                    )
                # Tracerfy's batch endpoint REQUIRES address + city + state on
                # every row, and a row missing one is not rejected loudly — it is
                # dropped from the upload. Production queue 162456: we sent 4 rows,
                # Tracerfy reported rows_uploaded=3. The dropped row then never
                # appears in the result CSV, so the ingest never matches it, so it
                # sits at 'submitted' forever and its lead reads "Processing" in the
                # UI indefinitely. Those rows were failed above, terminally and visibly.
                if unsubmittable:
                    msg = (
                        f"{len(unsubmittable)} {trace_type} row(s) dropped before "
                        "submit: missing address/city/state"
                    )
                    errors.append(msg)
                    _logger.warning("Dispatcher: %s", msg)
                if held:
                    _logger.info(
                        "Dispatcher: %d %s row(s) held: the same account's lookup for "
                        "that address is already under way", held, trace_type,
                    )
                if not rows:
                    # Nothing to claim: commit the failures and withdrawals on their
                    # own (no claim follows to carry them), and in every case end the
                    # transaction so no lock on a deferred row outlives this pass.
                    if unsubmittable or withdrawn:
                        db.commit()
                    else:
                        db.rollback()
                    continue

                # DURABLE CLAIM before the external POST (Codex High, 2026-09-02).
                # A row lock alone is not enough: if this process died after
                # Tracerfy accepted the batch but before the bookkeeping commit,
                # the rows rolled back to 'queued' and the next tick paid for them
                # again. Now the claim ('submitting') is committed first, so a
                # crash leaves the rows visibly claimed — never re-submitted —
                # and _alert_stale_claims pages ops to reconcile against
                # Tracerfy's queue list. Definite failures release the claim.
                payload_rows = _build_payload_rows(rows)
                claimed = [_Claim(r.id, r.result_id, r.job_id, r.user_id) for r in rows]
                claim_time = datetime.now(UTC)
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(
                        PendingSkipTraceRow.id.in_([c.id for c in claimed]),
                        PendingSkipTraceRow.status == "queued",
                    )
                    .values(status="submitting", submitted_at=claim_time)
                )
                db.commit()

                try:
                    response = submit_batch(payload_rows, trace_type=trace_type)
                except TracerfyError as exc:
                    msg = str(exc)
                    errors.append(msg)
                    _logger.warning("Tracerfy submit failed: %s", msg[:200])
                    kind = classify_submit_failure(msg)
                    if kind == "unknown_outcome":
                        # The request may have been delivered (e.g. read timeout
                        # after Tracerfy accepted). Leave the claim in place —
                        # re-submitting would double-pay; ops reconciles.
                        _logger.error(
                            "Tracerfy outcome UNKNOWN for %d %s rows — left 'submitting' "
                            "for reconciliation (never auto-resubmitted)", len(claimed), trace_type,
                        )
                        return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind)
                    if kind == "rate_limited":
                        _logger.warning(
                            "Tracerfy rate-limited (429) — backing off; %d %s rows "
                            "released to queued for the next tick", len(claimed), trace_type,
                        )
                        _release_claim(db, claimed, "queued")
                        return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind)
                    if kind == "out_of_credits":
                        _logger.error(
                            "Tracerfy OUT OF CREDITS (402) — add credits at tracerfy.com. "
                            "%d %s rows stay queued and will auto-submit once funded.",
                            len(claimed), trace_type,
                        )
                        # This condition stalled EVERY tenant's skip trace for 7+
                        # hours on 2026-09-02 with only a worker log line to show for
                        # it (565 rows / 7 jobs; the UI kept saying "Processing").
                        # Page ops (6h cooldown) and, when the 402 body says how far
                        # short we are, submit the affordable FIFO head so a partial
                        # top-up makes progress instead of being blocked by batch size.
                        _alert_out_of_credits(db, msg, trace_type, len(claimed))
                        affordable = affordable_row_count(msg, len(claimed), trace_type)
                        if not affordable:
                            _release_claim(db, claimed, "queued")
                            return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind)
                        _release_claim(db, claimed[affordable:], "queued")
                        claimed = claimed[:affordable]
                        payload_rows = payload_rows[:affordable]
                        try:
                            response = submit_batch(payload_rows, trace_type=trace_type)
                        except TracerfyError as exc2:
                            msg2 = str(exc2)
                            errors.append(msg2)
                            _logger.warning(
                                "Tracerfy partial resubmit (%d %s rows) failed: %s",
                                len(claimed), trace_type, msg2[:200],
                            )
                            kind2 = classify_submit_failure(msg2)
                            if kind2 == "unknown_outcome":
                                return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind2)
                            _release_claim(db, claimed, "errored" if kind2 == "provider_error" else "queued")
                            return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind2)
                        _logger.info(
                            "Tracerfy partial resubmit accepted: %d %s rows (credits-limited)",
                            len(claimed), trace_type,
                        )
                    elif kind == "provider_unavailable" or kind == "connection_error":
                        # Not delivered (connection refused/DNS) or Tracerfy 5xx:
                        # transient — release to queued, Beat retries in 5 min.
                        _release_claim(db, claimed, "queued")
                        return _tick_result(submitted_batches, submitted_rows, errors, deferred=kind)
                    else:
                        # Definite non-retryable rejection (bad batch / config):
                        # mark errored so it does not block future ticks.
                        _release_claim(db, claimed, "errored")
                        continue
                except Exception as exc:  # anything else after the POST may have gone out
                    errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
                    _logger.exception("Dispatcher: unexpected failure around Tracerfy submit")
                    return _tick_result(submitted_batches, submitted_rows, errors, deferred="unknown_outcome")

                queue_id = response["queue_id"]

                # PAST THIS LINE TRACERFY HAS ACCEPTED AND CHARGED FOR THE BATCH.
                # The bookkeeping below used to run unguarded (Codex, 2026-09-06):
                # any failure in it — a commit deadlock, a dropped connection, a
                # unique collision on tracerfy_queue_id — escaped the whole task
                # while the claim was already committed as 'submitting'. That left
                # a PAID remote queue with no local SkipTraceQueue row, and the
                # webhook that arrived later hit the ingest's 'unknown_queue'
                # no-op and discarded the results permanently. Production carries
                # 14 such orphaned Tracerfy queues (673 rows / 743 credits).
                #
                # The queue_id is now the one fact we refuse to lose: persist it,
                # retry once on a FRESH session (the first may be poisoned by the
                # failed transaction), and if even that fails, alert with the
                # queue_id so it can be adopted. _reconcile_stale_claims is the
                # backstop — it re-derives the association from Tracerfy's own
                # queue list on a later tick.
                try:
                    _persist_submission(
                        db, queue_id, claimed, trace_type, response,
                        claim_time=claim_time,
                    )
                except Exception as exc:  # noqa: BLE001 — a paid batch is at stake
                    _logger.error(
                        "Bookkeeping FAILED for accepted Tracerfy queue %s (%d rows): "
                        "%s — retrying on a fresh session",
                        queue_id, len(claimed), str(exc)[:200],
                    )
                    try:
                        db.rollback()
                    except Exception:  # noqa: BLE001 — session may already be dead
                        pass
                    if not _persist_submission_retry(
                        queue_id, claimed, trace_type, response, claim_time=claim_time,
                    ):
                        _alert_orphaned_queue(queue_id, trace_type, len(claimed))
                        errors.append(f"bookkeeping failed for queue {queue_id}")
                        return _tick_result(
                            submitted_batches, submitted_rows, errors,
                            deferred="bookkeeping_failed",
                        )

                submitted_batches += 1
                submitted_rows += len(claimed)
                _logger.info(
                    "Dispatcher: submitted %d rows (trace_type=%s, queue_id=%d)",
                    len(claimed), trace_type, queue_id,
                )
                break  # one batch per outer-loop iteration
            else:
                # No rows found in either trace_type — nothing to do
                break

    result = _tick_result(submitted_batches, submitted_rows, errors)
    if any(reconciled.get(k) for k in ("released", "adopted", "ambiguous")):
        result["reconciled"] = reconciled
    if submitted_batches or result.get("reconciled"):
        _logger.info("Dispatcher tick complete: %s", result)
    return result


class _Claim(NamedTuple):
    id: str
    result_id: str
    job_id: str
    user_id: str


def _job_delivered_sql(alias: str) -> str:
    """SQL: this job delivered its file, so its leads' lookups may be bought.

    Billing and the done-CAS commit together, so a billed job delivered whatever
    status a racing cancel wrote over it later (Codex review round 6). A failed
    or cancelled job created before BILLING_STAMP_RELIABLE_SINCE may have charged
    and delivered without a stamp (round 8). Binds ``:since``.
    """
    return (
        f"({alias}.status = 'done' OR {alias}.billing_applied_at IS NOT NULL "
        f" OR ({alias}.status IN ('failed', 'cancelled') AND {alias}.created_at < :since))"
    )


def _job_undelivered_sql(alias: str) -> str:
    """SQL: this job ended without delivering anything. Binds ``:since``."""
    return (
        f"({alias}.status IN ('failed', 'cancelled') AND {alias}.billing_applied_at IS NULL "
        f" AND {alias}.created_at >= :since)"
    )


def _atip_paid_allowed_sql():
    """SQL predicate: True unless this is an ATIP-named Tacoma lead with the paid switch off.

    The Python twin is skip_trace.code_violation_skip_trace_allowed; legal cleared NAMING
    that owner, not buying contact data keyed on the name.
    """
    from sqlalchemy import func
    from sqlalchemy import or_ as _or
    from sqlalchemy import true as _true

    from src.db.models import Result
    from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE as PIERCE_OWNER_SOURCE

    if settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED:
        return _true()
    return _or(
        func.coalesce(Result.enrichment_data.op("->>")("source"), "") != "tacoma_code_violations",
        func.coalesce(Result.enrichment_data.op("->>")("owner_source"), "") != PIERCE_OWNER_SOURCE,
    )


def _cancel_undeliverable_queued(db) -> int:
    """Cancel queued rows that must never be paid for. Returns rows cancelled.

    DOES NOT COMMIT, and does not swallow its own failure: it raises, and the
    caller owns both the transaction and what a failure means for the tick.
    That is not tidiness (Codex round 15, finding 15-5). From Phase 1b-2 a
    cancelled row must also move its contact-lookup-action disposition to
    'released' and append its audit event IN THE SAME TRANSACTION -- otherwise a
    crash between the two leaves an action reading "still looking" forever while
    the queue row is already gone, or an audit that disagrees with the queue.
    A helper that commits underneath its caller makes that atomicity impossible.

    A queued row is cancelled when its job ended failed/cancelled without billing
    (and after billing was stamped, see _job_undelivered_sql), or its lead is
    over quota, a same-run sibling or superseded row (an already-delivered lead is
    still traceable), or no longer 'queued' (for example a trace was copied
    onto it after it was enqueued). Only 'queued' rows are touched: 'submitting'
    and 'submitted' are already at Tracerfy and belong to the reconciler.

    The lead's status goes back to 'not_attempted' only when it is still 'queued'
    and no other active row still references it, so a later run can trace it if
    it becomes deliverable. Two statements, not one: a single statement's
    NOT EXISTS would still see the rows it is cancelling as 'queued'. Both are
    tenant-pinned (system session).
    """
    from sqlalchemy import text

    from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
    from src.api.results_category import skip_trace_eligible_sql
    from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE as PIERCE_OWNER_SOURCE
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

    cancelled = db.execute(
        text(
            "UPDATE pending_skip_trace_rows p SET status = 'cancelled' "  # noqa: S608 — fixed literals + bound params only
            "FROM jobs j, results r "
            "WHERE p.status = 'queued' "
            "  AND j.id = p.job_id AND j.user_id = p.user_id "
            "  AND r.id = p.result_id AND r.user_id = p.user_id "
            f"  AND ({_job_undelivered_sql('j')} "
            f"       OR NOT {skip_trace_eligible_sql('r')} "
            "       OR r.skip_trace_status <> 'queued' "
            "       OR COALESCE(r.enrichment_data->>:key, '') = :over_quota "
            # An ATIP-named Tacoma lead while PIERCE_CV_OWNER_SKIP_TRACE_ENABLED is
            # off: the name may be shown, not spent on. A row enqueued before the
            # switch was turned off is withdrawn here, before the submit loop that
            # follows. Only 'queued' rows are touched; 'submitting'/'submitted' are
            # already at Tracerfy and belong to the reconciler (Codex).
            "       OR (CAST(:atip_blocked AS boolean) "
            "           AND r.enrichment_data->>'source' = 'tacoma_code_violations' "
            "           AND r.enrichment_data->>'owner_source' = :atip_source)) "
            "RETURNING p.result_id, p.user_id"
        ),
        {"key": DELIVERY_EXCLUDED_KEY, "over_quota": OVER_QUOTA,
         "since": BILLING_STAMP_RELIABLE_SINCE,
         "atip_blocked": not settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED,
         "atip_source": PIERCE_OWNER_SOURCE},
    ).fetchall()
    if cancelled:
        db.execute(
            text(
                "UPDATE results r SET skip_trace_status = 'not_attempted' "
                "FROM unnest(CAST(:rids AS uuid[]), CAST(:uids AS uuid[])) "
                "     AS x(result_id, user_id) "
                "WHERE r.id = x.result_id AND r.user_id = x.user_id "
                "  AND r.skip_trace_status = 'queued' "
                "  AND NOT EXISTS (SELECT 1 FROM pending_skip_trace_rows o "
                "    WHERE o.result_id = r.id AND o.user_id = r.user_id "
                "      AND o.status IN ('queued', 'submitting', 'submitted'))"
            ),
            {"rids": [str(c.result_id) for c in cancelled],
             "uids": [str(c.user_id) for c in cancelled]},
        )
    return len(cancelled)


# ─── One answer, bought once ──────────────────────────────────────────────────
#
# Already-delivered leads are traceable (2026-09-18), so the same property of one
# account can now be queued by two runs: run 1's lookup still at Tracerfy when run 2
# queues its already-delivered row, or two runs on one date range at once. Each paid
# row is one credit, so everything below makes one (account, address) answer be
# bought once. The key is the per-tenant cache key the ingest writes the answer under,
# so nothing is ever held for, or copied from, another account.

# pg_try_advisory_xact_lock key: one tick at a time runs select -> claim commit, so the
# in-flight check cannot race another tick's uncommitted claim. Arbitrary constant.
_CLAIM_LOCK_KEY = 7_220_915_018


def _answer_key(row) -> str:
    """The per-tenant identity of the answer a pending row would buy: the exact key
    tracerfy_ingest writes it under (v2 lookup_subject_key over the pending row's
    OWN stored fields). Two rows with one key buy one answer.

    v2 (migration 098): the subject, not the address alone. The row records what
    was actually sent to the provider — address, trace type and the exact names —
    so two owners at one address are two answers and neither can be served the
    other's contacts.
    """
    from src.scrapers.enrichment.skip_trace import pending_row_subject_key

    return pending_row_subject_key(row)


def _submission_key(row) -> str:
    """What may not go out twice in ONE batch. NOT the answer key (round 14, 14-A).

    These are two different questions and the address-only design answered both
    with one key, which is why splitting them is easy to get wrong. The provider
    echoes back an address and nothing else, so `tracerfy_ingest` attributes on
    (address, city, state) and REFUSES the whole group when two answers arrive for
    one address. Under v2 two owners at one address are two subjects, so without
    this they would go out together, come back as two CSV rows, be refused
    together, and be charged with nothing to show for it.

    Global, no tenant: attribution has no tenant identifier either, so a
    cross-tenant pair at one address is refused exactly the same way.
    """
    from src.scrapers.enrichment.skip_trace import submission_collision_key

    return submission_collision_key(
        row.property_address, row.city, row.state, row.trace_type,
    )


def _fresh_answers(db, keys) -> dict:
    """The tenant cache entries for these answer keys that are still fresh (inside
    SKIP_TRACE_CACHE_DAYS, and not future-dated beyond clock skew). One definition for
    the sweep and the hold, so the two cannot disagree about "already answered"."""
    if not keys:
        return {}
    from sqlalchemy import select

    from src.db.models import SkipTraceCache

    now = datetime.now(UTC)
    return {
        c.address_hash: c
        for c in db.execute(
            select(SkipTraceCache).where(
                SkipTraceCache.address_hash.in_(set(keys)),
                SkipTraceCache.fetched_at
                >= now - timedelta(days=int(settings.SKIP_TRACE_CACHE_DAYS)),
                SkipTraceCache.fetched_at <= now + timedelta(minutes=5),
            )
        ).scalars()
    }


def _settle_queued_from_known_answers(db) -> int | None:
    """Settle queued rows this account already has an answer for. Never buys anything.

    - A fresh tenant cache entry (inside SKIP_TRACE_CACHE_DAYS): the answer is copied onto
      the lead, and the pending row becomes 'reused', a terminal state billing never
      counts (it bills 'completed' and 'unmatched' only). This is how a row held behind
      an in-flight twin gets that twin's answer once it lands.
    - The same address was charged but could not be attributed ('unmatched') inside the
      window: the lead is settled 'errored' and the pending row 'cancelled' rather than
      buying the same failure again (Codex).

    Runs under _CLAIM_LOCK_KEY, so no tick can claim a row this is settling. The lead is
    written only while it is still 'queued' and still eligible (delivered now or earlier,
    not over quota, not an ATIP-named lead while the paid switch is off: Codex round 2);
    a row that fails that is left queued for the cancel sweep. Commits (releasing the
    lock); best-effort like the cancel sweep: a failure leaves rows queued, and the hold
    in the claim path still refuses to buy an answer the cache already has.
    """
    from sqlalchemy import func, select, text, update

    from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
    from src.api.results_category import skip_trace_eligible_condition
    from src.db.models import PendingSkipTraceRow, Result

    now = datetime.now(UTC)
    window = timedelta(days=int(settings.SKIP_TRACE_CACHE_DAYS))
    try:
        if not db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY}
        ).scalar():
            db.rollback()
            return None
        queued = db.execute(
            select(PendingSkipTraceRow)
            .where(PendingSkipTraceRow.status == "queued")
            .order_by(PendingSkipTraceRow.enqueued_at)
            .limit(5000)
        ).scalars().all()
        if not queued:
            db.rollback()
            return 0
        keys = {p.id: _answer_key(p) for p in queued}
        cached = _fresh_answers(db, keys.values())
        charged_unanswered = {
            _answer_key(p)
            for p in db.execute(
                select(PendingSkipTraceRow).where(
                    PendingSkipTraceRow.user_id.in_({p.user_id for p in queued}),
                    PendingSkipTraceRow.status == "unmatched",
                    func.coalesce(
                        PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.enqueued_at
                    ) >= now - window,
                )
            ).scalars()
        }
        reused = failed = 0
        for p in queued:
            answer = cached.get(keys[p.id])
            if answer is None and keys[p.id] not in charged_unanswered:
                continue
            if answer is not None:
                values = {
                    "phone": answer.phone, "phone_type": answer.phone_type,
                    "phone_dnc_flag": answer.phone_dnc_flag, "email": answer.email,
                    "phones": answer.phones, "emails": answer.emails,
                    "skip_trace_status": "hit" if (answer.phone or answer.email) else "miss",
                    # When the data was OBTAINED, not now: this is the 365-day PII
                    # retention clock (settings.RETENTION_*), and a copy must not
                    # restart it (Master Review 2026-09-18).
                    "skip_trace_attempted_at": answer.fetched_at,
                    "skip_trace_source": "reused",  # no lookup bought for this row
                    # WHOSE answer this is (098): the subject of the pending row
                    # that is being settled, which is the key the answer was found
                    # under. Recorded now because it cannot be recovered later —
                    # party_name is rewritten by owner recovery, and a recomputed
                    # subject would name the current owner while these contacts
                    # belong to the previous one.
                    "skip_trace_subject_hash": keys[p.id],
                }
            else:
                values = {"skip_trace_status": "errored", "skip_trace_attempted_at": now}
            took = db.execute(
                update(Result)
                .where(
                    Result.id == p.result_id,
                    Result.user_id == p.user_id,  # tenant-pinned: system session
                    Result.skip_trace_status == "queued",
                    skip_trace_eligible_condition(),
                    func.coalesce(
                        Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
                    ) != OVER_QUOTA,
                    _atip_paid_allowed_sql(),
                )
                .values(**values)
            ).rowcount
            if not took:
                continue  # no longer eligible: the cancel sweep withdraws it
            db.execute(
                update(PendingSkipTraceRow)
                .where(PendingSkipTraceRow.id == p.id, PendingSkipTraceRow.status == "queued")
                .values(status="reused" if answer is not None else "cancelled")
            )
            if answer is not None:
                reused += 1
            else:
                failed += 1
        db.commit()
        if reused or failed:
            _logger.info(
                "Dispatcher: settled %d queued row(s) from this account's earlier answers "
                "and %d whose address was already charged without a match; none bought",
                reused, failed,
            )
        return reused + failed
    except Exception as exc:  # noqa: BLE001 - never break the submit loop
        db.rollback()
        _logger.warning("Dispatcher: known-answer sweep failed: %s", str(exc)[:160])
        return None


def _hold_answers_in_flight(db, rows: list) -> tuple[list, int]:
    """Keep one row per (account, address) in a batch, and none whose answer is already
    at Tracerfy or already in the tenant cache. The rest stay 'queued': the next tick's
    known-answer sweep settles them from the answer, or they go out then if the original
    failed without a charge.

    Called under _CLAIM_LOCK_KEY, so a concurrent tick's claim is either committed (and
    seen here as 'submitting') or not yet started.

    No time limit on the hold (Codex P1): a twin stuck 'submitting'/'submitted' may
    already be charged, and the system never auto-buys past an unknown outcome. The
    stuck row is paged (_alert_stale_claims) and resolved by the reconciler; the held
    twin follows it, settled from the answer or bought once the original is terminal
    without one."""
    if not rows:
        return rows, 0
    from sqlalchemy import select

    from src.db.models import PendingSkipTraceRow

    # The in-flight hold stays on the SUBJECT key and stays scoped to the tenants
    # in this batch: it exists so an account does not buy an answer it is already
    # buying, and one account's lookup must never hold (or answer) another's.
    #
    # It deliberately does NOT hold on the address. The attribution collision that
    # `_submission_key` guards is scoped to ONE batch -- `tracerfy_ingest` selects
    # its pending rows by `tracerfy_queue_id`, so rows in two different batches are
    # never weighed against each other and cannot make each other unattributable.
    # Holding across batches would buy nothing and would put one tenant behind
    # another tenant's lookup, which is exactly what
    # test_another_accounts_lookup_never_holds_or_answers_mine forbids.
    in_flight = {
        _answer_key(p)
        for p in db.execute(
            select(PendingSkipTraceRow).where(
                PendingSkipTraceRow.user_id.in_({r.user_id for r in rows}),
                PendingSkipTraceRow.status.in_(("submitting", "submitted")),
            )
        ).scalars()
    }
    # AFTER the in-flight read (Codex round 2): a twin's answer landing between the two
    # reads is caught by one of them, since ingest writes the cache and settles the twin
    # in one transaction. The known-answer sweep ran earlier, in its own transaction, so
    # without this a cache entry written since then would be bought a second time.
    answered = _fresh_answers(db, {_answer_key(r) for r in rows})
    keep: list = []
    seen: set[str] = set()
    seen_addresses: set[str] = set()
    for row in rows:
        key = _answer_key(row)
        if key in in_flight or key in answered or key in seen:
            continue
        # Same address already going out IN THIS BATCH, even for a different owner
        # and even for a different tenant: hold it. Two answers for one address
        # make _attribution_is_safe refuse the whole group, so sending both would
        # pay twice and answer nobody. This row stays 'queued' and goes out on a
        # later tick — and if the first answer lands in the cache meanwhile, the
        # known-answer sweep settles this one for free instead.
        addr_key = _submission_key(row)
        if addr_key in seen_addresses:
            continue
        seen.add(key)
        seen_addresses.add(addr_key)
        keep.append(row)
    return keep, len(rows) - len(keep)


def _tick_result(batches: int, rows: int, errors: list[str], deferred: str | None = None) -> dict:
    out = {"submitted_batches": batches, "submitted_rows": rows, "errors": errors}
    if deferred:
        out["deferred"] = deferred
    return out


def classify_submit_failure(message: str) -> str:
    """Map a TracerfyError message to what the dispatcher must do with its claim.

      rate_limited / out_of_credits / provider_unavailable (5xx) /
      connection_error (never delivered)  → release rows to 'queued'
      provider_error (definite 4xx / config) → mark 'errored'
      unknown_outcome (timeout, non-JSON or malformed 2xx) → KEEP the claim:
        Tracerfy may have accepted and charged the batch; re-submitting would
        double-pay, so ops reconciles against Tracerfy's queue list instead.
    """
    m = (message or "").lower()
    if "429" in m or "rate limit" in m:
        return "rate_limited"
    if "402" in m or "insufficient credit" in m:
        return "out_of_credits"
    if m.startswith("connection error"):
        return "connection_error"
    if m.startswith("network error") or "non-json" in m or "missing queue_id" in m:
        return "unknown_outcome"
    status = re.search(r"tracerfy returned (\d{3})", m)
    if status and status.group(1).startswith("5"):
        return "provider_unavailable"
    return "provider_error"


# ─── Pre-submit validation ────────────────────────────────────────────────────

# Tracerfy's POST /v1/api/trace/ documents address_column, city_column and
# state_column as required (docs/vendor/tracerfy-api.md). A row missing any of
# them is dropped from the upload rather than erroring the request, so the only
# way to notice is to count rows_uploaded against what was sent.
_REQUIRED_SUBMIT_FIELDS = ("property_address", "city", "state")


def row_is_submittable(row) -> bool:
    """True when a pending row carries every field Tracerfy requires.

    Pure and attribute-based so it can be unit-tested against a stub row without
    a database. `zip` is deliberately NOT required — Tracerfy documents it as
    optional and returns it from its own data when omitted.
    """
    return all(
        str(getattr(row, field, None) or "").strip()
        for field in _REQUIRED_SUBMIT_FIELDS
    )


def _partition_submittable(rows: list) -> tuple[list, list]:
    """Split rows into (submittable, unsubmittable), preserving FIFO order."""
    ok: list = []
    bad: list = []
    for r in rows:
        (ok if row_is_submittable(r) else bad).append(r)
    return ok, bad


def _fail_unsubmittable(db, rows: list) -> None:
    """Terminally fail rows Tracerfy would silently drop.

    Uses the existing 'errored' vocabulary on both the pending row and its
    Result — the same states _release_claim("errored") produces — so the UI
    renders "Error" instead of leaving the lead on "Processing" forever. These
    rows are never charged: they are stopped before the POST.

    Deliberately does NOT commit. The caller selected the FIFO head with
    `FOR UPDATE SKIP LOCKED` and holds those row locks until the claim is
    committed; committing here would release the locks on the *valid* rows in
    the same batch before they are claimed, letting a concurrent tick claim and
    submit them too (double pay). The caller commits this write together with
    the claim, or on its own when nothing submittable is left.
    """
    if not rows:
        return
    from sqlalchemy import tuple_, update

    from src.db.models import PendingSkipTraceRow, Result

    db.execute(
        update(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
        .values(status="errored")
    )
    # Same mandatory tenant pairing as the ingest path: the FIFO head this was
    # selected from spans tenants, so ids are pinned to their owner.
    db.execute(
        update(Result)
        .where(
            tuple_(Result.id, Result.user_id).in_(
                [(r.result_id, r.user_id) for r in rows]
            ),
            Result.skip_trace_status.in_(("queued", "submitted")),
        )
        .values(skip_trace_status="errored", skip_trace_attempted_at=datetime.now(UTC))
    )


def _partition_still_deliverable(db, rows: list) -> tuple[list, list, int]:
    """Split the FIFO head into (buy now, withdraw) and a count left for later.

    A lookup is bought only for a lead that was actually delivered: its Result is
    delivered by this run or an earlier one of the account, not excluded by the plan cap, and its job billed or reached
    'done'. Billing and the done-CAS commit together, so a billed job delivered
    its file whatever status was written over it later (Codex review round 6);
    'done' alone still counts for jobs that predate the billing stamp, and so
    does a failed or cancelled job created before BILLING_STAMP_RELIABLE_SINCE:
    it may have charged and delivered without a stamp (Codex review round 8).
    Rows are queued just before the enriched re-export and billing, so a job can
    still fail after queueing them (a failed upload, a failed refetch), and the
    flags can still change (a watchdog re-run repeats the survivor election and
    the cap). Paying Tracerfy for any of those buys contact data nobody receives.

    - buy now: billed, done, or pre-stamp terminal job; delivered now or earlier, not over
      quota.
    - withdraw: the job failed or was cancelled without billing after billing
      was stamped, or the lead is a same-run sibling or over quota. Never charged.
    - left for later (neither list): the job is still running, the Result row is
      locked by a writer right now, or it no longer exists (its pending row is
      CASCADE-deleted with it). Nothing is decided on a value that is changing.

    The Result rows are read FOR SHARE SKIP LOCKED and the share lock is held
    until the claim commits. SKIP LOCKED rather than a plain FOR SHARE: the
    dispatcher already holds the pending rows, and a purge cascading from a job
    locks results before pending rows, so waiting here would invert that order
    and deadlock (Codex). A skipped row is simply retried on the next tick, after
    the writer has committed, which also closes the read-then-claim race: no
    stale flag is ever acted on.

    The job row needs no lock: each job-side answer is final once seen. A job
    that billed stays billed, and a failed or cancelled job that never billed can
    never bill (the done-CAS only moves a non-terminal job).
    """
    if not rows:
        return [], [], 0
    from sqlalchemy import func, not_, select, tuple_

    from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
    from src.api.results_category import skip_trace_eligible_condition
    from src.db.models import Job, Result
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

    # Tenant-paired like every other write from this cross-tenant head.
    state = {
        (str(rid), str(uid)): (ineligible, excluded, status, billed, stamped)
        for rid, uid, ineligible, excluded, status, billed, stamped in db.execute(
            select(
                Result.id,
                Result.user_id,
                # A same-run sibling or superseded row; an already-delivered one stays.
                not_(skip_trace_eligible_condition()),
                # Same spelling as lead_actionability.actionable_condition; a NULL
                # blob yields NULL from ->>, which the COALESCE turns into ''.
                func.coalesce(
                    Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
                ) == OVER_QUOTA,
                Job.status,
                Job.billing_applied_at.is_not(None),
                Job.created_at >= BILLING_STAMP_RELIABLE_SINCE,
            )
            .join(Job, Job.id == Result.job_id)
            .where(
                tuple_(Result.id, Result.user_id).in_(
                    [(r.result_id, r.user_id) for r in rows]
                )
            )
            .with_for_update(read=True, skip_locked=True, of=Result)
        ).all()
    }
    keep: list = []
    drop: list = []
    later = 0
    for r in rows:
        seen = state.get((str(r.result_id), str(r.user_id)))
        if seen is None:
            later += 1
            continue
        ineligible, excluded, status, billed, stamped = seen
        terminal = status in ("failed", "cancelled")
        if ineligible or excluded or (terminal and not billed and stamped):
            drop.append(r)
        elif billed or status == "done" or terminal:
            keep.append(r)
        else:
            later += 1
    return keep, drop, later


def _cancel_undeliverable(db, rows: list) -> None:
    """Withdraw queued rows whose lead is no longer delivered. Never charged.

    The pending row becomes 'cancelled' and its Result goes back to
    'not_attempted', not 'errored': nothing failed, and if a later re-run makes
    the lead deliverable again the normal enqueue picks it up.

    Does NOT commit, for the reason spelled out in _fail_unsubmittable: the caller
    holds FOR UPDATE SKIP LOCKED on the whole head until the claim commits.
    """
    if not rows:
        return
    from sqlalchemy import tuple_, update

    from src.db.models import PendingSkipTraceRow, Result

    db.execute(
        update(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
        .values(status="cancelled")
    )
    db.execute(
        update(Result)
        .where(
            tuple_(Result.id, Result.user_id).in_(
                [(r.result_id, r.user_id) for r in rows]
            ),
            Result.skip_trace_status == "queued",
        )
        .values(skip_trace_status="not_attempted")
    )


# ─── Post-accept bookkeeping (a PAID batch depends on this) ───────────────────


def _persist_submission(
    db, queue_id: int, claimed: list, trace_type: str, response: dict,
    adopted: bool = False, claim_time=None,
) -> None:
    """Record an accepted Tracerfy batch: queue row + row/Result status flips.

    Idempotent by construction so the retry path (and the reconciler adoption)
    can re-run it safely: the SkipTraceQueue insert is ON CONFLICT DO NOTHING on
    the unique tracerfy_queue_id, and both updates are guarded on the status
    they expect to move from.

    `claim_time` is the instant the rows were claimed, committed BEFORE the POST
    went out. It is passed IN rather than computed here because everything
    computable at this point is later than the provider receiving the batch —
    including on the retry path, which runs on a fresh session after a failed
    commit and would otherwise mint a clock later still.

    `adopted` says WHICH path this is, and it exists because the two disagree
    about what time it is. On the live path we have just completed the POST, so
    `now` is a true send time. On the reconciler's adoption path the batch was
    sent on some earlier tick that never recorded it — often days ago — and the
    row is being inserted for the first time, so ON CONFLICT protects nothing
    and `now` is the adoption clock. Billing overage against that would place
    usage inside a subscription period that had not begun when the lookups ran.
    See migration 093.

    The pending rows KEEP their `submitted_at` (the claim time). It used to be
    restamped to `now` here, which moved a batch adopted days later into today's
    rolling window and made the daily spend cap count old spend as new (Codex,
    1b-1b consult C1). Only the queue row takes the bookkeeping clock. The row
    update is pinned to THIS claim (C2): `submitted_at = claim_time`, the batch's
    trace_type, and no queue id yet, so a delayed retry can never stamp this
    queue onto a row that was released and claimed again since.
    """
    from sqlalchemy import tuple_, update
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import PendingSkipTraceRow, Result, SkipTraceQueue

    now = datetime.now(UTC)
    first = claimed[0]

    provider_submitted_at = provider_submitted_time(response, claim_time, adopted)
    db.execute(
        pg_insert(SkipTraceQueue)
        .values(
            tracerfy_queue_id=queue_id,
            # NOTE: a batch is grouped by trace_type, not by tenant, so these two
            # describe the FIRST row only and are not the batch's owner. Ingest
            # correlates by tracerfy_queue_id and re-derives per-user attribution
            # from pending_skip_trace_rows, so nothing reads these for tenancy.
            job_id=first.job_id,
            user_id=first.user_id,
            trace_type=trace_type,
            status="pending",
            # Tracerfy de-duplicates identical addresses, so this can be smaller
            # than len(claimed) (prod: 25 sent → 24 uploaded → all 25 rows
            # reconciled by the webhook). Informational only.
            rows_uploaded=response.get("rows_uploaded") or len(claimed),
            credits_deducted=response.get("credits_deducted") or 0,
            # Normally filled in by the webhook receiver. On the RECONCILER's
            # adoption path the queue has often already completed, and carrying
            # its download_url here is what makes the ingest redrive recoverable
            # if the enqueue is lost (Codex).
            download_url=response.get("download_url"),
            submitted_at=now,
            provider_submitted_at=provider_submitted_at,
        )
        .on_conflict_do_nothing(index_elements=["tracerfy_queue_id"])
    )
    where = [
        PendingSkipTraceRow.id.in_([c.id for c in claimed]),
        PendingSkipTraceRow.status == "submitting",
        PendingSkipTraceRow.trace_type == trace_type,
        PendingSkipTraceRow.tracerfy_queue_id.is_(None),
    ]
    if claim_time is not None:
        where.append(PendingSkipTraceRow.submitted_at == claim_time)
    moved = db.execute(
        update(PendingSkipTraceRow)
        .where(*where)
        .values(status="submitted", tracerfy_queue_id=queue_id)
        .returning(PendingSkipTraceRow.id, PendingSkipTraceRow.result_id,
                   PendingSkipTraceRow.user_id)
    ).all()
    # Advance the matching Result rows 'queued' -> 'submitted' so the status
    # reflects "sent to Tracerfy, awaiting webhook" instead of sitting at
    # 'queued' (which reads as "not yet sent" — misleading for ops). The webhook
    # ingest matches by result_id (not status), so hit/miss reconciliation is
    # unaffected; the UI already renders 'submitted' the same as 'queued'.
    # Only the rows this claim actually moved, tenant-paired.
    if moved:
        db.execute(
            update(Result)
            .where(
                tuple_(Result.id, Result.user_id).in_(
                    [(m.result_id, m.user_id) for m in moved]
                ),
                Result.skip_trace_status == "queued",
            )
            .values(skip_trace_status="submitted")
        )
    # The queue row is committed even when some rows missed the pin. Tracerfy has
    # already accepted and charged this batch; rolling the queue back would make
    # ingest discard its results as 'unknown_queue' (1b-1b-i consult).
    db.commit()
    if len(moved) < len(claimed):
        missing = sorted({str(c.id) for c in claimed} - {str(m.id) for m in moved})
        _alert_partial_bookkeeping(queue_id, trace_type, len(claimed), missing)


def _persist_submission_retry(
    queue_id: int, claimed: list, trace_type: str, response: dict, claim_time=None
) -> bool:
    """Retry _persist_submission on a brand-new session. True when it stuck.

    Separate session because the caller's is likely poisoned (a failed commit
    leaves it in PendingRollbackError), and losing the queue_id is the one
    outcome worth a second connection.
    """
    try:
        from src.db.session import system_sync_session

        with system_sync_session() as db2:
            _persist_submission(
                db2, queue_id, claimed, trace_type, response, claim_time=claim_time,
            )
        _logger.info(
            "Bookkeeping recovered for Tracerfy queue %s on retry (%d rows)",
            queue_id, len(claimed),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — caller alerts on False
        _logger.error(
            "Bookkeeping retry ALSO failed for Tracerfy queue %s: %s",
            queue_id, str(exc)[:200],
        )
        return False


def _alert_orphaned_queue(queue_id: int, trace_type: str, n_rows: int) -> None:
    """Page ops about a PAID Tracerfy queue we could not record locally."""
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "skip_trace", f"orphaned_queue_{queue_id}",
            "Tracerfy batch accepted but NOT recorded: results will be lost",
            f"Tracerfy accepted (and charged for) queue_id={queue_id} "
            f"({trace_type}, {n_rows} rows) but BridgeLeads failed twice to write "
            f"the matching skip_trace_queues row. The completion webhook for this "
            f"queue will hit the ingest's 'unknown_queue' no-op and the paid "
            f"results will be discarded.\n\n"
            f"To recover: insert a skip_trace_queues row with "
            f"tracerfy_queue_id={queue_id} and stamp that id on the "
            f"pending_skip_trace_rows still in status='submitting' for this batch, "
            f"then replay the webhook (or re-ingest from the queue's download_url). "
            f"The rows are deliberately never auto-resubmitted, because that would pay twice.",
        )
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("orphaned-queue ops alert failed: %s", str(exc)[:120])


def _alert_partial_bookkeeping(
    queue_id: int, trace_type: str, n_claimed: int, missing_ids: list[str],
) -> None:
    """Page ops: a PAID queue was recorded, but some of its rows no longer
    belonged to the claim that sent them.

    Those rows missed the pinned update because they were released (and maybe
    claimed and sent again) after this batch went out. Tracerfy has their lookup
    from this batch; any newer claim of the same rows is a second purchase. This
    is detection, not prevention: nothing here can undo either charge.
    """
    _logger.error(
        "Bookkeeping for Tracerfy queue %s matched %d of %d %s row(s); %d had left "
        "this claim (possible double purchase): %s",
        queue_id, n_claimed - len(missing_ids), n_claimed, trace_type,
        len(missing_ids), ", ".join(missing_ids[:20]),
    )
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "skip_trace", f"partial_bookkeeping_{queue_id}",
            "Tracerfy batch recorded, but some rows had left their claim",
            f"Tracerfy accepted (and charged for) queue_id={queue_id} ({trace_type}, "
            f"{n_claimed} rows). The queue row IS recorded, so results will be "
            f"ingested for the {n_claimed - len(missing_ids)} row(s) attached to it. "
            f"The {len(missing_ids)} row(s) listed below are NOT attached: they were "
            f"released, and possibly claimed and submitted again, before this batch's "
            f"bookkeeping landed, so this batch's answer for them may be discarded and "
            f"a newer claim may be a second charge. Reconcile each by hand.\n\n"
            f"Row ids: {', '.join(missing_ids)}",
        )
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("partial-bookkeeping ops alert failed: %s", str(exc)[:120])


def _release_claim(db, claimed: list, to_status: str, claim_time=None) -> list:
    """Move claimed ('submitting') rows to `to_status`; no-op for an empty list.

    `claim_time` pins the release to ONE specific claim and is mandatory from the
    reconciler (Codex, 2026-09-07). `status == 'submitting'` alone is not enough:
    the reconciler reads stale claims without holding a lock, so between its read
    and its write another tick can release those rows, the dispatcher can re-claim
    them under a NEW submitted_at, and POST them. They are legitimately
    'submitting' again at that moment, so an unpinned release would free a batch
    that is already in flight — and the next tick would submit and pay for it a
    second time. Matching submitted_at makes the update a no-op unless the claim
    is still the exact one that was read.

    The submit path may pass claim_time=None: it holds the rows from its own
    claim commit through to the release with no window in between.

    Returns the rows ACTUALLY released, as (id, result_id, user_id). Anything
    that follows up on `results` must use that list, never `claimed`: a row that
    missed the pin belongs to a newer claim, and its lead is not ours to touch
    (1b-1b-i consult; the Result update below had exactly that bug).
    """
    if not claimed:
        return []
    from sqlalchemy import tuple_, update

    from src.db.models import PendingSkipTraceRow

    where = [
        PendingSkipTraceRow.id.in_([c.id for c in claimed]),
        PendingSkipTraceRow.status == "submitting",
    ]
    if claim_time is not None:
        where.append(PendingSkipTraceRow.submitted_at == claim_time)
    released = db.execute(
        update(PendingSkipTraceRow)
        .where(*where)
        .values(status=to_status, submitted_at=None)
        .returning(PendingSkipTraceRow.id, PendingSkipTraceRow.result_id,
                   PendingSkipTraceRow.user_id)
    ).all()
    if to_status == "errored" and released:
        # A definite rejection means these leads will never be traced; leaving
        # Result at 'queued' rendered "Processing" forever (Codex, 2026-09-02).
        # The UI already renders 'errored' as "Error".
        from src.db.models import Result

        db.execute(
            update(Result)
            .where(
                tuple_(Result.id, Result.user_id).in_(
                    [(r.result_id, r.user_id) for r in released]
                ),
                Result.skip_trace_status.in_(("queued", "submitted")),
            )
            .values(skip_trace_status="errored", skip_trace_attempted_at=datetime.now(UTC))
        )
    db.commit()
    return released


_STALE_CLAIM_AFTER = timedelta(minutes=30)

# How far a Tracerfy queue's created_at may sit from our claim commit and still
# be considered the same batch. The claim is committed immediately before the
# POST and submit_batch's timeout is 30s, so a real acceptance lands within
# seconds; the rest is clock skew. Deliberately MUCH tighter than the 5-minute
# beat interval so two consecutive ticks of the same trace_type can never fall
# into each other's window.
_RECONCILE_BEFORE = timedelta(seconds=60)
_RECONCILE_AFTER = timedelta(seconds=120)

# "Is this queue provably OURS?" and "is it provable that NO queue is ours?" are
# different questions and deserve different windows (Codex round 2). Adoption
# uses the tight window above, because adopting the wrong queue contaminates
# leads. RELEASING is the dangerous direction for money: release a claim whose
# batch Tracerfy actually accepted and the next tick pays for it twice. So a
# release additionally requires that no unaccounted-for queue of the same
# trace_type exists anywhere NEAR the claim — wide enough to absorb provider
# clock skew and a delayed created_at, which the tight window would not.
_RELEASE_QUIET_WINDOW = timedelta(minutes=30)


def _release_is_safe(
    candidates: list[dict], claim_time: datetime, trace_type: str, known_queue_ids: set
) -> bool:
    """True only when NO unrecorded queue of this trace_type sits near the claim.

    Absence of a tight-window match is not proof the batch was never accepted;
    absence of any nearby unaccounted queue is much closer to proof.
    """
    lo = claim_time - _RELEASE_QUIET_WINDOW
    hi = claim_time + _RELEASE_QUIET_WINDOW
    for q in candidates:
        if q.get("trace_type") != trace_type:
            continue
        if q.get("id") in known_queue_ids:
            continue  # already booked to some other batch
        created = _parse_tracerfy_ts(q.get("created_at"))
        if created is None or lo <= created <= hi:
            # Unparseable timestamps count AGAINST releasing: we cannot place
            # the queue, so we cannot rule it out.
            return False
    return True


def provider_submitted_time(response: dict, claim_time, adopted: bool):
    """When the PROVIDER got this batch, or None if we cannot defend an answer.

    Pure, so the rule can be tested without a database — this is the value skip
    trace overage is billed against, and getting it wrong charges a customer for
    usage in a period they never agreed to.

    Tracerfy's own ``created_at`` first. It is the provider's record of when it
    received the batch, it reads the same whether we ask now or on an adoption
    three days later, and it is therefore the only value correct on both paths.

    Failing that, ``claim_time`` — and specifically NOT `now`. This is the
    correction to the first version of this function, which used the moment
    bookkeeping ran and called it a lower bound. It is not one: bookkeeping runs
    AFTER the POST returns, and the provider can begin work the instant it
    receives the batch, so lookups can predate that value. The bookkeeping-retry
    path made it worse by computing a FRESH clock, later still (Codex).

    `claim_time` is taken before the rows are marked 'submitting' and committed
    BEFORE the POST is issued, so nothing the provider does can precede it. It is
    computed once per tick and passed in, so a retry cannot advance it.

    On the adoption path there is no defensible fallback at all: the batch went
    out on some earlier tick that never recorded it, and every clock still
    available is later than the work. That returns None, and the usage goes to a
    human.
    """
    ts = _parse_tracerfy_ts(response.get("created_at"))
    if ts is not None:
        if ts.tzinfo is None:
            # A naive value in a timestamptz column is read back in the server's
            # timezone, shifting the billing period by the offset. Tracerfy
            # sends Z-suffixed UTC; anything else is assumed to be the same.
            ts = ts.replace(tzinfo=UTC)
        return ts
    if adopted or claim_time is None:
        return None
    if claim_time.tzinfo is None:
        claim_time = claim_time.replace(tzinfo=UTC)
    return claim_time


def _parse_tracerfy_ts(value) -> datetime | None:
    """Parse Tracerfy's ISO-8601 created_at ('2026-09-06T09:28:11.189683Z')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def match_remote_queue(
    candidates: list[dict],
    claim_time: datetime,
    trace_type: str,
    n_claimed: int,
    known_queue_ids: set,
) -> tuple[str, dict | None]:
    """Decide what a stale claim's remote counterpart is. Pure, so it is testable.

    Returns (verdict, queue_or_None) where verdict is one of:
      "one"       - exactly one remote queue provably fits; adopt it.
      "none"      - Tracerfy holds no queue for this claim, so it was never
                    accepted and never charged; the claim is safe to release.
      "pending"   - a queue in our window is still processing and Tracerfy is
                    withholding its counts; it MAY be ours and it HAS been
                    charged, so defer and never release.
      "ambiguous" - more than one queue fits, or a completed and a pending one
                    both do; refuse and let a human look.

    The predicate is deliberately conservative (Codex review). Misattributing a
    queue is far worse than leaving rows stuck: adopting the wrong id would
    attach one batch's results to another batch's pending rows and bill the
    wrong tenants. Every clause below narrows toward "provably this batch":

      * same trace_type — normal and advanced are submitted ~0.6s apart, so
        this is what separates the pair;
      * created_at inside a tight window around the claim commit;
      * NOT already recorded locally — a queue that owns a skip_trace_queues
        row belongs to a batch that was booked successfully, never to this one;
      * queue_type == 'api' when Tracerfy reports it (never a UI upload);
      * 0 < rows_uploaded <= n_claimed — NEVER equality: Tracerfy de-duplicates
        identical addresses, so a 25-row batch legitimately uploads 24;
      * exactly one survivor, else refuse and let a human look.
    """
    hits, deferred = candidate_queues(candidates, claim_time, trace_type, n_claimed,
                                      known_queue_ids)
    if deferred and hits:
        # A completed candidate AND a still-pending one both fit. Undecidable
        # now and not self-resolving in a useful direction — get a human.
        return "ambiguous", None
    if deferred:
        # Tracerfy HIDES rows_uploaded/credits_deducted while an API queue is
        # still pending (docs/vendor/tracerfy-api.md). A pending queue in our
        # window therefore cannot be size-matched — and it may well be ours, and
        # it has already been charged. Reporting "none" here would release the
        # claim and the next tick would resubmit and pay a second time, which is
        # precisely what the durable claim exists to prevent. Defer instead: the
        # claim stays put and the next tick reconciles it once the queue
        # completes and its counts become visible.
        return "pending", None
    if not hits:
        return "none", None
    if len(hits) == 1:
        return "one", hits[0]
    return "ambiguous", None


def candidate_queues(
    candidates: list[dict],
    claim_time: datetime,
    trace_type: str,
    n_claimed: int,
    known_queue_ids: set,
) -> tuple[list[dict], list[dict]]:
    """Split remote queues into (size-matched hits, undecidable pending ones).

    Exposed separately from match_remote_queue so the reconciler can ask which
    queues a claim COULD match before deciding — two stale claims whose windows
    overlap must not both adopt the same queue.
    """
    lo = claim_time - _RECONCILE_BEFORE
    hi = claim_time + _RECONCILE_AFTER
    hits: list[dict] = []
    deferred: list[dict] = []
    for q in candidates:
        if q.get("trace_type") != trace_type:
            continue
        if q.get("id") in known_queue_ids:
            continue
        qtype = q.get("queue_type")
        if qtype is not None and qtype != "api":
            continue
        created = _parse_tracerfy_ts(q.get("created_at"))
        if created is None or not (lo <= created <= hi):
            continue
        uploaded = q.get("rows_uploaded")
        if q.get("pending") is True or not isinstance(uploaded, int):
            # Still processing, or counts withheld: undecidable, never "absent".
            deferred.append(q)
            continue
        if uploaded <= 0 or uploaded > n_claimed:
            continue
        hits.append(q)
    return hits, deferred


def contested_queue_ids(db, remote: list[dict], known: set) -> set:
    """Remote queue ids that could belong to MORE THAN ONE in-flight claim.

    Computed over every 'submitting' claim, fresh ones included (see the
    reconciler's comment). Shared by the live reconciler and
    scripts/repair_stuck_skip_trace_claims.py so the two can never disagree about
    which queues nobody may adopt.
    """
    from sqlalchemy import func, select

    from src.db.models import PendingSkipTraceRow

    all_claims = db.execute(
        select(
            PendingSkipTraceRow.submitted_at,
            PendingSkipTraceRow.trace_type,
            func.count().label("n"),
        )
        .where(
            PendingSkipTraceRow.status == "submitting",
            PendingSkipTraceRow.submitted_at.isnot(None),
        )
        .group_by(PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.trace_type)
    ).all()
    contested: set = set()
    seen_once: set = set()
    for c_time, c_type, c_n in all_claims:
        hits, deferred = candidate_queues(remote, c_time, c_type, c_n, known)
        for q in hits + deferred:
            qid = q.get("id")
            (contested if qid in seen_once else seen_once).add(qid)
    return contested


def _reconcile_stale_claims(db) -> dict:
    """Resolve claims stuck mid-submission against Tracerfy's own queue list.

    The dispatcher commits a durable claim BEFORE the POST and refuses to
    auto-resubmit an unknown outcome, because re-sending a batch Tracerfy already
    accepted pays for it twice. That safety left the rows parked in 'submitting'
    forever waiting for a human to reconcile — and the ops alert asking for that
    human goes nowhere while OPS_ALERT_EMAIL is unset. Production accumulated 637
    such rows across 15 jobs and 3 users, stuck up to four days, every one of
    them reading "Processing" in the UI.

    The reconciliation is mechanical, so do it mechanically. Rows claimed in the
    same tick share a submitted_at, which groups them back into their batch. For
    each group ask Tracerfy what exists:

      no matching queue  -> it never accepted the batch, we were never charged:
                            release to 'queued' and let the next tick send it.
      exactly one match  -> it accepted (and charged): adopt the queue_id so the
                            batch is recorded, and re-drive ingest if the queue
                            has already completed — that is what recovers a
                            result set whose webhook hit 'unknown_queue'.
      more than one      -> refuse to guess. Alert and leave the rows alone.

    Never resubmits. Returns a small summary for the tick result.
    """
    summary = {"released": 0, "adopted": 0, "ambiguous": 0, "deferred": 0, "groups": 0}
    try:
        from sqlalchemy import func, select

        from src.db.models import PendingSkipTraceRow, SkipTraceQueue
        from src.scrapers.enrichment.skip_trace import TracerfyError, fetch_queues

        _redrive_unigested_adoptions(db)

        cutoff = datetime.now(UTC) - _STALE_CLAIM_AFTER
        groups = db.execute(
            select(
                PendingSkipTraceRow.submitted_at,
                PendingSkipTraceRow.trace_type,
                func.count().label("n"),
            )
            .where(
                PendingSkipTraceRow.status == "submitting",
                PendingSkipTraceRow.submitted_at.isnot(None),
                PendingSkipTraceRow.submitted_at < cutoff,
            )
            .group_by(PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.trace_type)
            .order_by(PendingSkipTraceRow.submitted_at)
        ).all()
        if not groups:
            return summary
        summary["groups"] = len(groups)

        try:
            remote = fetch_queues()
        except TracerfyError as exc:
            _logger.error(
                "Stale-claim reconciliation could not reach Tracerfy (%d group(s) "
                "left claimed): %s", len(groups), str(exc)[:200],
            )
            _alert_stale_claims(db)
            return summary

        known = {
            r[0] for r in db.execute(select(SkipTraceQueue.tracerfy_queue_id))
        }

        # A queue that COULD belong to more than one stale claim must not be
        # adopted by either (Codex). The dispatcher can submit two batches of the
        # same trace_type seconds apart within one tick (max_batches > 1), so
        # their windows overlap — and `rows_uploaded <= n_claimed` is a subset
        # test, not identity. A 500-row claim that never reached Tracerfy would
        # otherwise happily adopt the 100-row queue belonging to the claim beside
        # it, attaching those results to the wrong leads and billing the wrong
        # tenants. Contested queues are refused for every claimant.
        # Contention must be computed against EVERY in-flight claim, not just the
        # stale ones (Codex, 2026-09-07). A claim that is still fresh has not
        # recorded its queue id yet, so that queue is absent from `known` and sits
        # inside a stale neighbour's window — the stale claim would adopt the
        # FRESH claim's queue, attaching its results to the wrong leads and
        # billing the wrong tenants. Widening the scan to all 'submitting' rows
        # makes such a queue contested, and contested queues are adopted by nobody.
        contested = contested_queue_ids(db, remote, known)

        for claim_time, trace_type, n in groups:
            verdict, queue = match_remote_queue(
                remote, claim_time, trace_type, n, known
            )
            if verdict == "one" and queue.get("id") in contested:
                _logger.error(
                    "Reconciliation: Tracerfy queue %s fits MORE THAN ONE stale "
                    "claim — refusing to adopt it for any of them",
                    queue.get("id"),
                )
                verdict, queue = "ambiguous", None
            rows = db.execute(
                select(PendingSkipTraceRow).where(
                    PendingSkipTraceRow.status == "submitting",
                    PendingSkipTraceRow.submitted_at == claim_time,
                    PendingSkipTraceRow.trace_type == trace_type,
                )
            ).scalars().all()
            if not rows:
                continue
            claimed = [_Claim(r.id, r.result_id, r.job_id, r.user_id) for r in rows]

            if verdict == "pending":
                # Accepted (and charged) but still processing, so Tracerfy is
                # withholding its counts. Leave the claim exactly where it is.
                summary["deferred"] += len(claimed)
                _logger.info(
                    "Reconciliation: a still-pending Tracerfy queue may own the %d "
                    "%s row(s) claimed at %s — deferring, never releasing",
                    len(claimed), trace_type, claim_time,
                )
            elif verdict == "none":
                if not _release_is_safe(remote, claim_time, trace_type, known):
                    # An unaccounted-for queue of this trace_type sits near the
                    # claim without fitting it precisely. It may still be ours
                    # under clock skew, and it has been charged — releasing
                    # would resubmit and pay twice. Hold and let a human look.
                    summary["ambiguous"] += len(claimed)
                    _logger.error(
                        "Reconciliation: no exact match for the %d %s row(s) claimed "
                        "at %s, but an unrecorded %s queue sits nearby — refusing to "
                        "release (double-charge risk)",
                        len(claimed), trace_type, claim_time, trace_type,
                    )
                    _alert_ambiguous_reconciliation(len(claimed), trace_type, claim_time)
                    continue
                _logger.warning(
                    "Reconciliation: no Tracerfy queue for the %d %s row(s) claimed "
                    "at %s — never accepted, never charged. Releasing to 'queued'.",
                    len(claimed), trace_type, claim_time,
                )
                _release_claim(db, claimed, "queued", claim_time=claim_time)
                summary["released"] += len(claimed)
            elif verdict == "one":
                queue_id = queue["id"]
                _logger.warning(
                    "Reconciliation: adopting Tracerfy queue %s for the %d %s row(s) "
                    "claimed at %s (uploaded=%s, credits=%s)",
                    queue_id, len(claimed), trace_type, claim_time,
                    queue.get("rows_uploaded"), queue.get("credits_deducted"),
                )
                # Persist the download_url with the adoption so the redrive below
                # is RECOVERABLE. Without it a failed .delay() (broker blip, or the
                # process dying right after this commit) would leave the queue
                # recorded — and therefore excluded from every future
                # reconciliation pass — with nothing ever ingesting it (Codex).
                # claim_time pins the adoption to exactly this claim (C2); billing
                # is unaffected, provider_submitted_time ignores it when adopted.
                _persist_submission(
                    db, queue_id, claimed, trace_type, queue, adopted=True,
                    claim_time=claim_time,
                )
                known.add(queue_id)
                summary["adopted"] += len(claimed)
                _redrive_completed_queue(queue)
            else:
                summary["ambiguous"] += len(claimed)
                _logger.error(
                    "Reconciliation AMBIGUOUS for the %d %s row(s) claimed at %s — "
                    "multiple Tracerfy queues fit. Refusing to guess.",
                    len(claimed), trace_type, claim_time,
                )
                _alert_ambiguous_reconciliation(len(claimed), trace_type, claim_time)
    except Exception as exc:  # noqa: BLE001 — reconciliation must never break the tick
        _logger.exception("Stale-claim reconciliation failed: %s", str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return summary


def _redrive_unigested_adoptions(db) -> None:
    """Re-enqueue ingest for adopted queues whose redrive never landed.

    Adoption commits the queue row (with its download_url) and THEN enqueues the
    ingest best-effort. If that enqueue is lost — broker blip, or the process
    dying immediately after the commit — the queue is now recorded, and being
    recorded excludes it from every future reconciliation pass, so nothing would
    ever ingest it (Codex). A queue still 'pending' locally while already
    carrying a download_url is exactly that state; ingest is idempotent, so a
    redundant redrive costs nothing.
    """
    try:
        from sqlalchemy import select

        from src.db.models import SkipTraceQueue

        rows = db.execute(
            select(
                SkipTraceQueue.tracerfy_queue_id,
                SkipTraceQueue.download_url,
                SkipTraceQueue.rows_uploaded,
                SkipTraceQueue.credits_deducted,
            ).where(
                SkipTraceQueue.status == "pending",
                SkipTraceQueue.download_url.isnot(None),
            )
        ).all()
        for qid, url, uploaded, credits in rows:
            _logger.warning(
                "Reconciliation: re-driving ingest for adopted queue %s whose "
                "first enqueue was lost", qid,
            )
            _redrive_completed_queue({
                "id": qid,
                "pending": False,
                "download_url": url,
                "rows_uploaded": uploaded or 0,
                "credits_deducted": credits or 0,
            })
    except Exception as exc:  # noqa: BLE001 — never break the tick
        _logger.warning("adoption redrive sweep failed: %s", str(exc)[:120])


def _redrive_completed_queue(queue: dict) -> None:
    """Re-run ingest for an adopted queue that Tracerfy already finished.

    Its completion webhook fired while no local skip_trace_queues row existed,
    so the ingest took its 'unknown_queue' no-op and the paid results were
    dropped. Now that the row exists the ingest can run; it is idempotent (it
    locks the queue row and no-ops once completed/billed), so a redundant
    re-drive is harmless.
    """
    if queue.get("pending") is not False:
        return  # still processing — its webhook will arrive normally
    download_url = queue.get("download_url")
    if not download_url:
        # Completed but no URL to fetch: nothing can ingest it, and because the
        # queue is now recorded it is excluded from future reconciliation AND
        # from the redrive sweep (which requires a download_url). That is a
        # silent dead end, so say so loudly (Codex, 2026-09-07).
        _logger.error(
            "Reconciliation adopted completed Tracerfy queue %s but it carries NO "
            "download_url — its paid results cannot be ingested automatically",
            queue.get("id"),
        )
        try:
            from src.workers.ops_alerts import send_ops_alert

            send_ops_alert(
                "skip_trace", f"adopted_no_url_{queue.get('id')}",
                "Adopted skip-trace batch has no download URL",
                f"Tracerfy queue {queue.get('id')} completed and was adopted, but "
                f"the queue list carries no download_url, so nothing can ingest "
                f"its results. The batch was charged. Fetch the queue directly "
                f"(GET /v1/api/queue/{queue.get('id')}) to recover it by hand.",
            )
        except Exception as exc:  # noqa: BLE001 — alerting is best-effort
            _logger.warning("adopted-no-url alert failed: %s", str(exc)[:120])
        return
    try:
        from src.workers.tracerfy_ingest import ingest_tracerfy_batch

        ingest_tracerfy_batch.delay(
            queue_id=queue["id"],
            download_url=download_url,
            rows_uploaded=queue.get("rows_uploaded", 0),
            credits_deducted=queue.get("credits_deducted", 0),
        )
        _logger.info(
            "Reconciliation: re-drove ingest for completed Tracerfy queue %s",
            queue["id"],
        )
    except Exception as exc:  # noqa: BLE001 — adoption already persisted
        _logger.error(
            "Reconciliation adopted queue %s but could not enqueue ingest: %s "
            "— the queue row exists, so a webhook replay will still recover it",
            queue.get("id"), str(exc)[:200],
        )


def _alert_ambiguous_reconciliation(n_rows: int, trace_type: str, claim_time) -> None:
    """Page ops when more than one Tracerfy queue could be a stale claim's."""
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "skip_trace", f"ambiguous_reconcile_{claim_time}",
            "Skip-trace reconciliation ambiguous: needs a human",
            f"{n_rows} pending_skip_trace_rows claimed at {claim_time} "
            f"({trace_type}) match MORE THAN ONE Tracerfy queue, so the "
            f"reconciler refused to adopt one. Picking wrong would attach this "
            f"batch's results to another batch's leads and bill the wrong "
            f"tenants.\n\nResolve by hand: compare the candidate queues' "
            f"addresses via GET /v1/api/queue/:id against these rows, then "
            f"either stamp the right tracerfy_queue_id on them (status "
            f"'submitted') or set them back to 'queued'. They are never "
            f"auto-resubmitted, because that would pay twice.",
        )
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("ambiguous-reconcile ops alert failed: %s", str(exc)[:120])


def _alert_stale_claims(db) -> None:
    """Page ops when rows have sat in 'submitting' longer than any POST could take:
    a dispatcher died mid-handoff (or the outcome was unknown). They are never
    auto-resubmitted — reconcile against GET /v1/api/queues/ on Tracerfy."""
    try:
        from sqlalchemy import func, select

        from src.db.models import PendingSkipTraceRow
        from src.workers.ops_alerts import send_ops_alert

        cutoff = datetime.now(UTC) - _STALE_CLAIM_AFTER
        stale, oldest = db.execute(
            select(func.count(), func.min(PendingSkipTraceRow.submitted_at)).where(
                PendingSkipTraceRow.status == "submitting",
                PendingSkipTraceRow.submitted_at < cutoff,
            )
        ).one()
        if not stale:
            return
        _logger.error("Skip trace: %d rows stuck in 'submitting' since %s", stale, oldest)
        send_ops_alert(
            "skip_trace", "stale_claims",
            "Skip trace rows stuck mid-submission: reconcile with Tracerfy",
            f"{stale} pending_skip_trace_rows have status='submitting' for more than "
            f"{int(_STALE_CLAIM_AFTER.total_seconds() // 60)} minutes (oldest claim {oldest}). "
            "A dispatcher tick died between the Tracerfy POST and its bookkeeping, or the "
            "POST outcome was unknown. Check Tracerfy's queue list for a batch of that size "
            "around that time: if it exists, stamp its queue_id on the rows (status "
            "'submitted'); if not, set them back to 'queued'. They are deliberately never "
            "re-submitted automatically (double-pay risk).",
        )
    except Exception as exc:  # alerting is best-effort; never break the tick
        _logger.warning("stale-claim check failed: %s", str(exc)[:120])


# Tracerfy 402 body (observed 2026-09-02): {"error":"Insufficient credits for
# normal trace. You need 226 more credits to complete this request. ..."}
_NEED_MORE_CREDITS_RE = re.compile(r"need\s+(\d+)\s+more\s+credits?", re.IGNORECASE)


def affordable_row_count(error_message: str, n_rows: int, trace_type: str) -> int:
    """How many of an `n_rows` batch the account can still pay for, derived from
    Tracerfy's 402 "You need N more credits" message. 0 when the message does
    not carry the shortfall (unknown → do not guess) or nothing is affordable.

    Pure so it is unit-testable; the dispatcher slices the FIFO head
    `rows[:affordable]` and the matching payload together. An unknown trace_type
    raises (credits_for) rather than being priced at 1 credit (Codex ii-b S4).
    """
    per_row = credits_for(trace_type)
    m = _NEED_MORE_CREDITS_RE.search(error_message or "")
    if not m or n_rows <= 0:
        return 0
    shortfall = int(m.group(1))
    available = n_rows * per_row - shortfall
    if available <= 0:
        return 0
    return min(n_rows, available // per_row)


def _alert_out_of_credits(db, message: str, trace_type: str, batch_size: int) -> None:
    """Page ops once per cooldown window with the size of the stalled backlog."""
    try:
        from sqlalchemy import func, select

        from src.db.models import PendingSkipTraceRow
        from src.workers.ops_alerts import send_ops_alert

        # The rejected batch is still 'submitting' at this point — count it too.
        queued = db.execute(
            select(func.count(), func.count(func.distinct(PendingSkipTraceRow.job_id)))
            .where(PendingSkipTraceRow.status.in_(("queued", "submitting")))
        ).one()
        send_ops_alert(
            "skip_trace", "out_of_credits",
            "Tracerfy out of credits: skip trace stalled",
            f"Tracerfy rejected a {batch_size}-row {trace_type} batch with 402. "
            f"{queued[0]} rows across {queued[1]} jobs are queued and will not be "
            f"traced until the account is topped up at tracerfy.com. "
            f"Users see 'Processing' on every affected lead until then.\n\n"
            f"Provider message: {message[:300]}",
        )
    except Exception as exc:  # alerting is best-effort; never break the tick
        _logger.warning("out-of-credits ops alert failed: %s", str(exc)[:120])


def _build_payload_rows(pending_rows: list) -> list[dict]:
    """Convert PendingSkipTraceRow ORM instances into Tracerfy-shaped dicts."""
    out = []
    for r in pending_rows:
        out.append({
            "address": r.property_address or "",
            "city": r.city or "",
            "state": r.state or "",
            "zip": r.zip or "",
            "first_name": r.first_name or "",
            "last_name": r.last_name or "",
            "mail_address": r.mail_address or "",
            "mail_city": r.mail_city or "",
            "mail_state": r.mail_state or "",
            "mailing_zip": r.mail_zip or "",
        })
    return out
