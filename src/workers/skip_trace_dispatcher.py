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

_logger = setup_logger("worker.skip_trace_dispatcher")


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

    from sqlalchemy import and_, select, update

    from src.db.models import PendingSkipTraceRow
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import TracerfyError, submit_batch

    max_batches = max(1, settings.SKIP_TRACE_MAX_BATCHES_PER_TICK)
    submitted_batches = 0
    submitted_rows = 0
    errors: list[str] = []

    # SYSTEM SESSION: the dispatcher drains pending rows across all
    # tenants in a single pass (Tracerfy batches are grouped by
    # trace_type, not by user). This is a legitimate cross-tenant
    # system operation.
    with system_sync_session() as db:
        # Resolve anything stuck mid-submission from a previous tick BEFORE
        # draining new work: a released claim rejoins the FIFO head below and
        # goes out in this same tick instead of waiting another five minutes.
        reconciled = _reconcile_stale_claims(db)

        for _ in range(max_batches):
            # Pick a trace_type to drain this pass. Prefer 'normal' first
            # (cheaper per row), then 'advanced'. Within a pass we batch
            # one trace_type only because Tracerfy takes trace_type as a
            # top-level field on the POST.
            for trace_type in ("normal", "advanced"):
                rows = (
                    db.execute(
                        select(PendingSkipTraceRow)
                        .where(
                            and_(
                                PendingSkipTraceRow.status == "queued",
                                PendingSkipTraceRow.trace_type == trace_type,
                            )
                        )
                        .order_by(PendingSkipTraceRow.enqueued_at)
                        .limit(5000)  # Tracerfy handles large batches; cap for safety
                        # Lock the FIFO head so a concurrent tick (beat double-fire
                        # across a redeploy, a tick outliving its interval) skips
                        # these rows instead of reading the same 'queued' set.
                        .with_for_update(skip_locked=True)
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    continue

                # Tracerfy's batch endpoint REQUIRES address + city + state on
                # every row, and a row missing one is not rejected loudly — it is
                # dropped from the upload. Production queue 162456: we sent 4 rows,
                # Tracerfy reported rows_uploaded=3. The dropped row then never
                # appears in the result CSV, so the ingest never matches it, so it
                # sits at 'submitted' forever and its lead reads "Processing" in the
                # UI indefinitely. Fail it HERE — terminally and visibly — instead of
                # shipping it to be silently discarded.
                rows, unsubmittable = _partition_submittable(rows)
                if unsubmittable:
                    _fail_unsubmittable(db, unsubmittable)
                    msg = (
                        f"{len(unsubmittable)} {trace_type} row(s) dropped before "
                        "submit: missing address/city/state"
                    )
                    errors.append(msg)
                    _logger.warning("Dispatcher: %s", msg)
                if not rows:
                    # Nothing submittable left: commit the failures on their own
                    # (no claim follows to carry them).
                    if unsubmittable:
                        db.commit()
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
                    _persist_submission(db, queue_id, claimed, trace_type, response)
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
                    if not _persist_submission_retry(queue_id, claimed, trace_type, response):
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
    from sqlalchemy import update

    from src.db.models import PendingSkipTraceRow, Result

    db.execute(
        update(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
        .values(status="errored")
    )
    db.execute(
        update(Result)
        .where(
            Result.id.in_([r.result_id for r in rows]),
            Result.skip_trace_status.in_(("queued", "submitted")),
        )
        .values(skip_trace_status="errored", skip_trace_attempted_at=datetime.now(UTC))
    )


# ─── Post-accept bookkeeping (a PAID batch depends on this) ───────────────────


def _persist_submission(db, queue_id: int, claimed: list, trace_type: str, response: dict) -> None:
    """Record an accepted Tracerfy batch: queue row + row/Result status flips.

    Idempotent by construction so the retry path (and a future reconciler
    adoption) can re-run it safely: the SkipTraceQueue insert is ON CONFLICT DO
    NOTHING on the unique tracerfy_queue_id, and both updates are guarded on the
    status they expect to move from.
    """
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import PendingSkipTraceRow, Result, SkipTraceQueue

    now = datetime.now(UTC)
    first = claimed[0]
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
        )
        .on_conflict_do_nothing(index_elements=["tracerfy_queue_id"])
    )
    db.execute(
        update(PendingSkipTraceRow)
        .where(
            PendingSkipTraceRow.id.in_([c.id for c in claimed]),
            PendingSkipTraceRow.status == "submitting",
        )
        .values(status="submitted", tracerfy_queue_id=queue_id, submitted_at=now)
    )
    # Advance the matching Result rows 'queued' -> 'submitted' so the status
    # reflects "sent to Tracerfy, awaiting webhook" instead of sitting at
    # 'queued' (which reads as "not yet sent" — misleading for ops). The webhook
    # ingest matches by result_id (not status), so hit/miss reconciliation is
    # unaffected; the UI already renders 'submitted' the same as 'queued'.
    db.execute(
        update(Result)
        .where(
            Result.id.in_([c.result_id for c in claimed]),
            Result.skip_trace_status == "queued",
        )
        .values(skip_trace_status="submitted")
    )
    db.commit()


def _persist_submission_retry(
    queue_id: int, claimed: list, trace_type: str, response: dict
) -> bool:
    """Retry _persist_submission on a brand-new session. True when it stuck.

    Separate session because the caller's is likely poisoned (a failed commit
    leaves it in PendingRollbackError), and losing the queue_id is the one
    outcome worth a second connection.
    """
    try:
        from src.db.session import system_sync_session

        with system_sync_session() as db2:
            _persist_submission(db2, queue_id, claimed, trace_type, response)
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
            "Tracerfy batch accepted but NOT recorded — results will be lost",
            f"Tracerfy accepted (and charged for) queue_id={queue_id} "
            f"({trace_type}, {n_rows} rows) but BridgeLeads failed twice to write "
            f"the matching skip_trace_queues row. The completion webhook for this "
            f"queue will hit the ingest's 'unknown_queue' no-op and the paid "
            f"results will be discarded.\n\n"
            f"To recover: insert a skip_trace_queues row with "
            f"tracerfy_queue_id={queue_id} and stamp that id on the "
            f"pending_skip_trace_rows still in status='submitting' for this batch, "
            f"then replay the webhook (or re-ingest from the queue's download_url). "
            f"The rows are deliberately never auto-resubmitted — that would pay twice.",
        )
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("orphaned-queue ops alert failed: %s", str(exc)[:120])


def _release_claim(db, claimed: list, to_status: str) -> None:
    """Move claimed ('submitting') rows to `to_status`; no-op for an empty list."""
    if not claimed:
        return
    from sqlalchemy import update

    from src.db.models import PendingSkipTraceRow

    db.execute(
        update(PendingSkipTraceRow)
        .where(
            PendingSkipTraceRow.id.in_([c.id for c in claimed]),
            PendingSkipTraceRow.status == "submitting",
        )
        .values(status=to_status, submitted_at=None)
    )
    if to_status == "errored":
        # A definite rejection means these leads will never be traced; leaving
        # Result at 'queued' rendered "Processing" forever (Codex, 2026-09-02).
        # The UI already renders 'errored' as "Error".
        from src.db.models import Result

        db.execute(
            update(Result)
            .where(
                Result.id.in_([c.result_id for c in claimed]),
                Result.skip_trace_status.in_(("queued", "submitted")),
            )
            .values(skip_trace_status="errored", skip_trace_attempted_at=datetime.now(UTC))
        )
    db.commit()


_STALE_CLAIM_AFTER = timedelta(minutes=30)

# How far a Tracerfy queue's created_at may sit from our claim commit and still
# be considered the same batch. The claim is committed immediately before the
# POST and submit_batch's timeout is 30s, so a real acceptance lands within
# seconds; the rest is clock skew. Deliberately MUCH tighter than the 5-minute
# beat interval so two consecutive ticks of the same trace_type can never fall
# into each other's window.
_RECONCILE_BEFORE = timedelta(seconds=60)
_RECONCILE_AFTER = timedelta(seconds=120)


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
        contested: set = set()
        seen_once: set = set()
        for claim_time, trace_type, n, *_ in groups:
            hits, deferred = candidate_queues(remote, claim_time, trace_type, n, known)
            for q in hits + deferred:
                qid = q.get("id")
                (contested if qid in seen_once else seen_once).add(qid)

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
                _logger.warning(
                    "Reconciliation: no Tracerfy queue for the %d %s row(s) claimed "
                    "at %s — never accepted, never charged. Releasing to 'queued'.",
                    len(claimed), trace_type, claim_time,
                )
                _release_claim(db, claimed, "queued")
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
                _persist_submission(db, queue_id, claimed, trace_type, queue)
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
            "Skip-trace reconciliation ambiguous — needs a human",
            f"{n_rows} pending_skip_trace_rows claimed at {claim_time} "
            f"({trace_type}) match MORE THAN ONE Tracerfy queue, so the "
            f"reconciler refused to adopt one — picking wrong would attach this "
            f"batch's results to another batch's leads and bill the wrong "
            f"tenants.\n\nResolve by hand: compare the candidate queues' "
            f"addresses via GET /v1/api/queue/:id against these rows, then "
            f"either stamp the right tracerfy_queue_id on them (status "
            f"'submitted') or set them back to 'queued'. They are never "
            f"auto-resubmitted — that would pay twice.",
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
            "Skip trace rows stuck mid-submission — reconcile with Tracerfy",
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


_CREDITS_PER_ROW = {"normal": 1, "advanced": 2}
# Tracerfy 402 body (observed 2026-09-02): {"error":"Insufficient credits for
# normal trace. You need 226 more credits to complete this request. ..."}
_NEED_MORE_CREDITS_RE = re.compile(r"need\s+(\d+)\s+more\s+credits?", re.IGNORECASE)


def affordable_row_count(error_message: str, n_rows: int, trace_type: str) -> int:
    """How many of an `n_rows` batch the account can still pay for, derived from
    Tracerfy's 402 "You need N more credits" message. 0 when the message does
    not carry the shortfall (unknown → do not guess) or nothing is affordable.

    Pure so it is unit-testable; the dispatcher slices the FIFO head
    `rows[:affordable]` and the matching payload together.
    """
    m = _NEED_MORE_CREDITS_RE.search(error_message or "")
    if not m or n_rows <= 0:
        return 0
    per_row = _CREDITS_PER_ROW.get(trace_type, 1)
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
            "Tracerfy out of credits — skip trace stalled",
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
