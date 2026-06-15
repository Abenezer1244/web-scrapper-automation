"""Body logic for the flush_skip_trace_meter_outbox beat task."""

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _flush_skip_trace_meter_outbox_impl() -> None:
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
