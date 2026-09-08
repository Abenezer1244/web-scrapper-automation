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
        # Only rows whose OWNER has a Stripe customer id. A user without one
        # cannot be sent a MeterEvent, and report_skip_trace_meter_event no
        # longer writes the row off for that reason, so without this join every
        # such row would be re-enqueued every three minutes forever. The join is
        # a HOLD, not a write-off: the moment the user checks out and
        # stripe_customer_id lands, the row is picked up on the next sweep and
        # billed. Joining `users` rather than reading the row's own snapshot is
        # the whole point, because the snapshot is what was stale.
        rows = db.execute(
            text("""
                SELECT e.id
                FROM skip_trace_meter_events e
                JOIN users u ON u.id = e.user_id
                WHERE e.reported_at IS NULL
                  AND e.created_at < NOW() - INTERVAL '30 seconds'
                  AND u.stripe_customer_id IS NOT NULL
                  AND u.stripe_customer_id <> ''
            """)
        ).fetchall()
        # Held rows are invisible otherwise. Nothing else in the system says
        # "there is real usage here that nobody can be charged for", and the
        # write-off this replaced at least had the virtue of being loud once.
        held = db.execute(
            text("""
                SELECT COUNT(*), COALESCE(SUM(e.billable_units), 0)
                FROM skip_trace_meter_events e
                JOIN users u ON u.id = e.user_id
                WHERE e.reported_at IS NULL
                  AND e.created_at < NOW() - INTERVAL '1 hour'
                  AND (u.stripe_customer_id IS NULL OR u.stripe_customer_id = '')
            """)
        ).fetchone()

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

    held_rows, held_units = (held[0], held[1]) if held else (0, 0)
    if held_rows:
        _logger.warning(
            "Skip-trace meter outbox: %d event(s) totalling %d billable unit(s) "
            "are held because their owner has no Stripe customer id",
            held_rows, held_units,
        )
        try:
            from src.workers.ops_alerts import send_ops_alert

            send_ops_alert(
                kind="billing",
                key="skip_trace_meter_held",
                subject="Skip-trace usage that cannot be billed",
                body=(
                    f"{held_rows} skip-trace meter event(s) totalling "
                    f"{held_units} billable unit(s) are waiting on a Stripe "
                    "customer id. The usage is real and the rows are kept, not "
                    "written off: they bill automatically once the account "
                    "checks out. If these accounts are never going to "
                    "subscribe, the usage is a cost with no revenue against it."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - alerting must never break the beat
            _logger.error("held-meter alert failed: %s", str(exc)[:200])
