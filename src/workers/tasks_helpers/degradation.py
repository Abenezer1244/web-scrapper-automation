"""A job that reaches DONE although one of its phases failed (P0-07).

run_scrape_job catches some phase failures and carries on to DONE: the customer
still gets their export, billed for the rows actually delivered. That used to be
log-only, so a run that silently delivered fewer or less complete leads looked
like any other. record_degraded() makes it durable and visible, BEFORE the
job's DONE write:

  1. a job_degradations row (migration 117) and one customer job-log warning,
     committed together in one transaction;
  2. then an ops alert (its cooldown is per job and reason).

If step 1 cannot be written the caller must not finish the job as a normal DONE:
DegradationNotRecordedError propagates to run_scrape_job's failure path.

Nothing here stores or sends exception text, only its class name (P0-04).
"""

from __future__ import annotations

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger("worker.task")

# Keep in step with alembic/versions/117_job_degradations.py _REASONS.
# reason -> the customer's job-log line. Plain words: what they may be missing.
REASONS: dict[str, str] = {
    "enrichment": (
        "Address lookup stopped early. Some leads may be missing an address or "
        "owner details, and contact lookup did not run for this search."
    ),
    "nts_match": (
        "Auction details could not be added yet. They are filled in on the next "
        "daily update; your download will include them then."
    ),
    "survivor_reelection": (
        "Some duplicate records could not be merged, so a few leads may be missing "
        "from this search."
    ),
    "claim_transfer": (
        "Some leads were marked as already delivered by mistake and may be missing "
        "from this search."
    ),
    "skip_trace_enqueue": (
        "Contact lookup could not start for this search. It will be retried "
        "automatically."
    ),
    "property_key": "Property matching across your lists is incomplete for this search.",
    "property_membership": "Property matching across your lists is incomplete for this search.",
    "owner_flags": "Absentee and out-of-state owner flags may be missing for this search.",
    "claim_release": (
        "Some records without an address may be hidden from future searches."
    ),
}

# Only this reason is repaired automatically (the 6b beat); the rest alert and
# are recovered by hand with the existing backfill scripts.
_REPAIRED_AUTOMATICALLY = {"skip_trace_enqueue"}


class DegradationNotRecordedError(RuntimeError):
    """The degraded marker could not be committed; the job must not finish as DONE."""


def record_degraded(db, r, job_id: str, reason: str, exc: BaseException | None) -> bool:
    """Durably mark ``job_id`` degraded for ``reason``; tell the customer and ops.

    Call it AFTER the failing phase's ``db.rollback()``. Idempotent per
    (job, reason): a repeat writes nothing and sends nothing. Returns True when
    this call recorded it. Raises DegradationNotRecordedError if the row and job-log
    line cannot be committed.
    """
    import json
    import uuid

    from src.db.models import JobLog
    from src.workers.tasks_helpers.status import _now

    if reason not in REASONS:
        raise ValueError(f"unknown degradation reason {reason!r}")
    exc_type = type(exc).__name__[:64] if exc is not None else None
    repair = "pending" if reason in _REPAIRED_AUTOMATICALLY else "none"
    log_id = str(uuid.uuid4())
    try:
        inserted = db.execute(
            text(
                "INSERT INTO job_degradations (job_id, reason, exc_type, repair_status, "
                "next_attempt_at) VALUES (:job_id, :reason, :exc_type, :repair, "
                "CASE WHEN :repair = 'pending' THEN now() END) "
                "ON CONFLICT (job_id, reason) DO NOTHING RETURNING id"
            ),
            {"job_id": job_id, "reason": reason, "exc_type": exc_type, "repair": repair},
        ).first()
        if inserted is None:
            db.rollback()
            return False
        # The marker and the customer's job-log line land in one transaction.
        db.add(JobLog(id=log_id, job_id=job_id, level="warning", message=REASONS[reason]))
        db.commit()
    except Exception as write_exc:  # noqa: BLE001 — re-raised as a typed refusal
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        _logger.error("Job %s: degraded marker %s NOT recorded: %s",
                      job_id, reason, type(write_exc).__name__)
        raise DegradationNotRecordedError(f"job {job_id}: {reason} marker not recorded") from None

    _logger.warning("Job %s: degraded (%s): %s", job_id, reason, exc_type)
    # Committed: everything below is best effort and never undoes the record. The
    # live stream gets the same payload _publish_log sends; a viewer who misses it
    # still reads the stored line on reload.
    try:
        r.publish(f"job_logs:{job_id}", json.dumps({
            "id": log_id, "level": "warning", "message": REASONS[reason],
            "created_at": _now().isoformat(), "type": "log",
        }))
    except Exception as pub_exc:  # noqa: BLE001 — the line is stored; only the push failed
        _logger.warning("Job %s: degraded log publish failed: %s", job_id, type(pub_exc).__name__)
    from src.workers.ops_alerts import send_ops_alert

    send_ops_alert(
        "job_degraded", f"{job_id}:{reason}",
        f"Job degraded: {reason}",
        # Not "reached DONE": a later phase can still fail the attempt, and a retry
        # may then complete. The marker is the history of this phase failing.
        f"Job {job_id}: its '{reason}' phase failed ({exc_type or 'unknown'}) and the "
        f"run carried on. The customer's job log says so. "
        + ("Repair is automatic (skip-trace re-enqueue beat)." if repair == "pending"
           else "Recover with the matching backfill script if the customer needs it."),
    )
    return True
