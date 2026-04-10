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

from datetime import UTC, datetime

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

    from src.db.models import PendingSkipTraceRow, SkipTraceQueue
    from src.db.session import SyncSessionLocal
    from src.scrapers.enrichment.skip_trace import TracerfyError, submit_batch

    max_batches = max(1, settings.SKIP_TRACE_MAX_BATCHES_PER_TICK)
    submitted_batches = 0
    submitted_rows = 0
    errors: list[str] = []

    with SyncSessionLocal() as db:
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
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    continue

                payload_rows = _build_payload_rows(rows)
                try:
                    response = submit_batch(payload_rows, trace_type=trace_type)
                except TracerfyError as exc:
                    # On rate-limit (429) we stop this tick entirely — Beat
                    # will retry in 5 min. On other errors, mark these rows
                    # errored so they don't block future ticks indefinitely.
                    msg = str(exc)
                    errors.append(msg)
                    _logger.warning("Tracerfy submit failed: %s", msg[:200])
                    if "429" in msg or "rate limit" in msg.lower():
                        _logger.info("Rate-limited, backing off until next tick")
                        return {
                            "submitted_batches": submitted_batches,
                            "submitted_rows": submitted_rows,
                            "errors": errors,
                        }
                    # Non-rate-limit error: mark batch errored + continue
                    db.execute(
                        update(PendingSkipTraceRow)
                        .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
                        .values(status="errored")
                    )
                    db.commit()
                    continue

                queue_id = response["queue_id"]
                now = datetime.now(UTC)

                # Record the Tracerfy queue for webhook correlation.
                # We associate with the first row's job_id; webhook
                # ingest looks up all rows by tracerfy_queue_id anyway.
                first_row = rows[0]
                queue_record = SkipTraceQueue(
                    tracerfy_queue_id=queue_id,
                    job_id=first_row.job_id,
                    user_id=first_row.user_id,
                    trace_type=trace_type,
                    status="pending",
                    rows_uploaded=response.get("rows_uploaded", len(rows)),
                    credits_deducted=0,  # filled in by webhook receiver
                    submitted_at=now,
                )
                db.add(queue_record)

                # Mark the PendingSkipTraceRow rows as submitted + stamp
                # with the tracerfy queue_id for webhook correlation.
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
                    .values(
                        status="submitted",
                        tracerfy_queue_id=queue_id,
                        submitted_at=now,
                    )
                )
                db.commit()

                submitted_batches += 1
                submitted_rows += len(rows)
                _logger.info(
                    "Dispatcher: submitted %d rows (trace_type=%s, queue_id=%d)",
                    len(rows), trace_type, queue_id,
                )
                break  # one batch per outer-loop iteration
            else:
                # No rows found in either trace_type — nothing to do
                break

    result = {
        "submitted_batches": submitted_batches,
        "submitted_rows": submitted_rows,
        "errors": errors,
    }
    if submitted_batches:
        _logger.info("Dispatcher tick complete: %s", result)
    return result


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
