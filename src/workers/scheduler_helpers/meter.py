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
        # First, un-hold the ONE review reason that can stop being true.
        #
        # `no_customer_id` means "this user had no Stripe customer when we
        # looked". That is mutable local state — it becomes false the moment
        # they check out — so unlike every other review reason it does not need
        # a human, it needs asking again. Left alone it was a one-way sink: the
        # sweep only re-enqueues `pending`, so those rows sat in review forever
        # and quietly underbilled usage that had become recoverable (Codex).
        #
        # Only rows whose owner NOW has a customer id are moved back, and they
        # are moved to `pending`, NOT to billable: the full gate still runs and
        # can refuse them again for any of its own reasons. Having a customer id
        # is permission to re-ask the question, never an answer to it.
        requeued = db.execute(
            text("""
                UPDATE skip_trace_meter_events e
                   SET disposition = 'pending',
                       disposition_at = NULL,
                       disposition_reason = NULL
                  FROM users u
                 WHERE u.id = e.user_id
                   AND e.disposition = 'needs_review'
                   AND e.disposition_reason = 'no_customer_id'
                   AND u.stripe_customer_id IS NOT NULL
                   AND u.stripe_customer_id <> ''
                RETURNING e.id
            """)
        ).fetchall()
        if requeued:
            db.commit()
            _logger.info(
                "Skip-trace meter sweep: %d row(s) held for a missing Stripe "
                "customer now have one — re-queued for the gate to decide again",
                len(requeued),
            )

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
        # Broken down by REASON, with no age floor.
        #
        # A 24h floor was here to stop the alert firing every three minutes. It
        # is the wrong instrument: `created_at` is the OUTBOX clock, so usage
        # reconciled 34 days after it was incurred would wait another day before
        # anyone heard about it — and Stripe stops accepting a backdated event at
        # 35. The floor could therefore spend the last of the window it was
        # supposed to protect (Codex). Repetition is already handled properly by
        # send_ops_alert's own cooldown on (billing, skip_trace_meter_held), so
        # surfacing a row immediately costs nothing and buys back the days.
        #
        # The per-reason split means the message says what actually needs doing
        # rather than guessing at one cause for all of them.
        reasons = db.execute(
            text("""
                SELECT COALESCE(e.disposition_reason, 'unspecified') AS reason,
                       COUNT(*) AS n,
                       COALESCE(SUM(e.billable_units), 0) AS units,
                       MIN(e.created_at) AS oldest
                FROM skip_trace_meter_events e
                WHERE e.disposition = 'needs_review'
                GROUP BY 1
                ORDER BY units DESC
            """)
        ).fetchall()
        held = (
            sum(r.n for r in reasons),
            sum(r.units for r in reasons),
        )

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
                    f"{held_units} unit(s) are waiting for a decision."
                    "\n\nBy reason (oldest row first seen):\n"
                    + "\n".join(
                        f"  {r.reason}: {r.n} row(s), {r.units} unit(s), "
                        f"oldest {r.oldest:%Y-%m-%d}"
                        for r in reasons
                    )
                    + "\n\nThe usage is real. It does not bill on its own, "
                    "deliberately: something about it could not be tied to a "
                    "billing agreement that covered it, so sending it would "
                    "charge a customer for usage in a period or at a rate they "
                    "never agreed to. The reason above says which."
                    "\n\nTIME LIMIT: Stripe refuses a meter event backdated "
                    "more than 35 days, so a row left here long enough can no "
                    "longer be billed through the meter at all and has to be "
                    "recovered on an invoice by hand."
                    + "\n\nSettle or write them off with:\n"
                    "  python scripts/settle_skip_trace_meter_rows.py --list"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - alerting must never break the beat
            _logger.error("held-meter alert failed: %s", str(exc)[:200])
