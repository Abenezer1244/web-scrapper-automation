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
    from sqlalchemy import exists as sa_exists

    from src.api.lead_actionability import actionable_condition
    from src.db.models import PendingSkipTraceRow, Result, SkipTraceQueue
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
        _alert_stale_claims(db)

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
                                # Never spend on a row that is no longer a
                                # deliverable lead. Rows are enqueued during
                                # inline enrichment, BEFORE the plan cap marks
                                # anything, so a finite-quota user's over-quota
                                # rows can already be sitting here. The cap
                                # withdraws its own queued rows, but this is the
                                # guard at the moment money is actually spent, so
                                # it also covers rows queued by any other path
                                # and any race between the two (Codex).
                                sa_exists()
                                .where(
                                    and_(
                                        Result.id == PendingSkipTraceRow.result_id,
                                        actionable_condition(),
                                    )
                                )
                                .correlate(PendingSkipTraceRow),
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
                now = datetime.now(UTC)
                ids = [c.id for c in claimed]

                # Record the Tracerfy queue for webhook correlation.
                # We associate with the first row's job_id; webhook
                # ingest looks up all rows by tracerfy_queue_id anyway.
                first = claimed[0]
                queue_record = SkipTraceQueue(
                    tracerfy_queue_id=queue_id,
                    job_id=first.job_id,
                    user_id=first.user_id,
                    trace_type=trace_type,
                    status="pending",
                    # Tracerfy de-duplicates identical addresses, so this can be
                    # smaller than len(claimed) (prod: 25 sent → 24 uploaded → all
                    # 25 rows reconciled by the webhook). Informational only.
                    rows_uploaded=response.get("rows_uploaded", len(claimed)),
                    credits_deducted=0,  # filled in by webhook receiver
                    submitted_at=now,
                )
                db.add(queue_record)

                # Mark the claimed rows submitted + stamp the tracerfy queue_id
                # for webhook correlation.
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(
                        PendingSkipTraceRow.id.in_(ids),
                        PendingSkipTraceRow.status == "submitting",
                    )
                    .values(
                        status="submitted",
                        tracerfy_queue_id=queue_id,
                        submitted_at=now,
                    )
                )
                # Advance the matching Result rows 'queued' -> 'submitted' so the
                # status reflects "sent to Tracerfy, awaiting webhook" instead of
                # sitting at 'queued' (which reads as "not yet sent" — misleading
                # for ops). The webhook ingest matches by result_id (not status),
                # so hit/miss reconciliation is unaffected; the UI already renders
                # 'submitted' the same as 'queued' ("Processing").
                db.execute(
                    update(Result)
                    .where(
                        Result.id.in_([c.result_id for c in claimed]),
                        Result.skip_trace_status == "queued",
                    )
                    .values(skip_trace_status="submitted")
                )
                db.commit()

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
    if submitted_batches:
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
