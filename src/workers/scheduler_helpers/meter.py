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
        # `disposition = 'pending'` and nothing else.
        #
        # This used to join `users` and require a stripe_customer_id, which was
        # THE P1: create_checkout writes that id when the checkout SESSION is
        # created, before payment, so merely starting checkout released a user's
        # entire pre-subscription backlog. It also could not distinguish "not
        # sent yet" from "decided not billable", because both were reported_at
        # IS NULL.
        #
        # The sweep no longer decides anything. It re-enqueues pending rows and
        # the task applies assert_billable at report time — one rule, one place.
        # A row this sweep picks up may well come back non_billable, and that is
        # the correct outcome, not a wasted pass.
        rows = db.execute(
            text("""
                SELECT e.id
                FROM skip_trace_meter_events e
                WHERE e.disposition = 'pending'
                  AND e.created_at < NOW() - INTERVAL '30 seconds'
            """)
        ).fetchall()
        # Held rows are invisible otherwise. Nothing else in the system says
        # "there is real usage here that nobody can be charged for", and the
        # write-off this replaced at least had the virtue of being loud once.
        # What a human still has to decide. `needs_review` only: non_billable
        # has already been decided, and alerting on a settled decision every
        # three minutes is how an alert becomes noise nobody reads.
        held = db.execute(
            text("""
                SELECT COUNT(*), COALESCE(SUM(e.billable_units), 0)
                FROM skip_trace_meter_events e
                WHERE e.disposition = 'needs_review'
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
            "Skip-trace meter outbox: %d event(s) totalling %d unit(s) need a "
            "human decision before they can be billed",
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
                    f"{held_units} unit(s) are marked needs_review: the usage "
                    "is real, but no billing agreement covered it that we can "
                    "charge against automatically — most often because the "
                    "usage is older than Stripe's 35-day backdating limit. "
                    "These do NOT bill on their own, deliberately. Sending them "
                    "with today's date would charge a customer for usage at a "
                    "rate and in a period they never agreed to. Query "
                    "skip_trace_meter_events WHERE disposition = 'needs_review' "
                    "and settle or write them off explicitly."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - alerting must never break the beat
            _logger.error("held-meter alert failed: %s", str(exc)[:200])
