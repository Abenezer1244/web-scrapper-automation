"""Skip-trace metered billing: report usage to Stripe after successful ingest.

Called by the Tracerfy webhook receiver after a batch's rows are ingested.
For each user whose records were in the batch:

  1. Count the ingested rows attributable to that user
  2. Determine the bundled quota for the user's plan (0 for pro,
     1000 for business, 2000 for agency, starter is blocked upstream)
  3. Read `users.skip_trace_used_this_month`
  4. Compute billable_units = max(0, (used + new) - quota)
  5. If billable_units > 0, call stripe.billing.MeterEvent.create
     with payload {"value": billable_units, "stripe_customer_id": cus_xxx}
  6. Increment the counter by the number of new lookups

Reports only BILLABLE (above-quota) units. Lookups below the quota are
absorbed into the base subscription price.

If STRIPE_SECRET_KEY or the skip-trace Product/Meter IDs are unset, this
function logs a warning and no-ops — skip trace can run without billing
attached, useful for testing.
"""
from datetime import UTC, datetime

from sqlalchemy import text

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("api.billing.skip_trace_usage")


def _stripe_enabled() -> bool:
    return bool(
        settings.STRIPE_SECRET_KEY
        and settings.STRIPE_PRODUCT_SKIP_TRACE
        and settings.STRIPE_METER_SKIP_TRACE
    )


def report_lookups_for_user(
    db,
    user_id: str,
    new_lookups: int,
    queue_id: int,
) -> dict:
    """Advance a user's counter (in the caller's txn) and compute Stripe units.

    REDTEAM B2: this function used to db.commit() the counter advance and
    then call Stripe, swallowing Stripe errors — so a re-entry/replay could
    re-advance the counter and the counter/meter could diverge. It is now
    split into two phases tied to the caller's idempotency:

      Phase 1 (here, NO commit): under the row lock the caller already holds,
      advance skip_trace_used_this_month and compute billable_units. The
      caller commits this in the SAME transaction that marks the queue
      "completed"/billed, so a replay (which finds the queue already
      completed and no-ops before reaching billing — see REDTEAM B1) can
      never re-advance the counter.

      Phase 2 (report_meter_event_to_stripe, called AFTER the caller's
      commit): fire the Stripe MeterEvent with a stable (queue_id, user_id)
      identifier so Stripe dedupes its own retries.

    Args:
        db: SQLAlchemy session (must be in an active transaction OWNED by the
            caller — this function never commits or rolls back).
        user_id: UUID string of the user whose rows were ingested
        new_lookups: Number of rows in the completed Tracerfy batch that
            were attributable to this user
        queue_id: Tracerfy queue_id for the batch. REQUIRED — used to
            build a stable Stripe MeterEvent identifier so webhook
            replay dedupes on Stripe's side. H12 from the full-SaaS
            review.

    Returns:
        Dict with keys:
            plan: the user's current plan
            quota: the bundled monthly quota for that plan
            used_before: counter value before this ingest
            used_after: counter value after this ingest
            billable_units: units to report to Stripe (0 if all bundled)
            stripe_customer_id: customer id for the deferred Stripe call (or None)
            meter_event_id: always None here — set by phase 2 after commit
            error: str if the user could not be advanced (no counter change)
    """
    if new_lookups <= 0:
        return {"billable_units": 0, "meter_event_id": None}

    # Read the user's plan + current counter + Stripe customer ID
    user_row = db.execute(
        text("""
            SELECT plan, stripe_customer_id, skip_trace_used_this_month,
                   skip_trace_period_start
            FROM users
            WHERE id = :uid
            FOR UPDATE
        """),
        {"uid": user_id},
    ).fetchone()
    if not user_row:
        _logger.warning("report_lookups_for_user: user %s not found", user_id)
        return {"billable_units": 0, "meter_event_id": None, "error": "user_not_found"}

    plan = (user_row.plan or "starter").lower()
    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS.get(plan, 0)
    used_before = user_row.skip_trace_used_this_month or 0
    used_after = used_before + new_lookups

    # Reset the counter if the billing period has rolled (month boundary).
    # We only track the start; the monthly reset task clears it. As a
    # defensive belt-and-suspenders, if period_start is null or >35 days
    # old, treat this as a fresh period.
    now = datetime.now(UTC)
    period_start = user_row.skip_trace_period_start
    if period_start is None or (now - period_start).days > 35:
        _logger.info("Resetting skip-trace period for user %s", user_id)
        used_before = 0
        used_after = new_lookups
        db.execute(
            text("UPDATE users SET skip_trace_period_start = :now WHERE id = :uid"),
            {"now": now, "uid": user_id},
        )

    # Compute billable units — only the portion ABOVE the bundled quota
    before_above = max(0, used_before - quota)
    after_above = max(0, used_after - quota)
    billable_units = after_above - before_above

    # REDTEAM B2: advance the counter but do NOT commit here. The caller
    # (the Tracerfy ingest worker) owns the transaction and commits this
    # counter advance in the SAME transaction that flips the SkipTraceQueue
    # row to "completed" under the lock it already holds. Tying the advance
    # to that once-only status flip means a replayed webhook — which sees
    # status="completed" and no-ops before reaching billing — can never
    # re-advance the counter. Committing here (the old H11 behaviour) broke
    # that coupling and made the counter pumpable on re-entry; it also
    # committed the counter independently of the Stripe meter call below,
    # so counter and meter could diverge whenever Stripe errored.
    db.execute(
        text("""
            UPDATE users
            SET skip_trace_used_this_month = :used
            WHERE id = :uid
        """),
        {"used": used_after, "uid": user_id},
    )

    result = {
        "plan": plan,
        "quota": quota,
        "used_before": used_before,
        "used_after": used_after,
        "billable_units": billable_units,
        # Carry the customer id so the caller can fire the Stripe meter
        # event AFTER it commits — we deliberately do not touch the network
        # while holding the caller's row lock.
        "stripe_customer_id": user_row.stripe_customer_id,
        "meter_event_id": None,
    }

    if billable_units <= 0:
        _logger.info(
            "User %s used %d/%d bundled lookups (no overage this batch)",
            user_id[:8], used_after, quota,
        )

    return result


class _StripeNotConfiguredError(Exception):
    """Stripe billing is not wired up — a non-retryable skip, not a failure.

    REDTEAM (Codex convergence — meter outbox): distinguished from transient
    Stripe/network errors so the durable report path can treat "Stripe off"
    as a terminal no-op (stamp reported_at, stop) while still RAISING on real
    transient failures so Celery autoretry kicks in.
    """


def report_meter_event_to_stripe(
    user_id: str,
    queue_id: int,
    billable_units: int,
    stripe_customer_id: str | None,
    plan: str,
) -> str | None:
    """Fire ONE Stripe skip-trace MeterEvent — RAISES on transient failure.

    REDTEAM (Codex convergence — meter outbox): this used to CATCH every
    Stripe exception and return out["error"] normally. The durable report
    task relies on autoretry_for=(Exception,), so a swallowed exception acked
    the task "successful" and a transient Stripe failure was NEVER retried —
    usage advanced locally but never billed. It now RAISES on any Stripe/
    network failure so the calling task retries, and only treats genuinely
    terminal conditions (Stripe not configured, no customer id, nothing to
    bill) as no-ops via the typed _StripeNotConfiguredError signal / a None return.

    The identifier is the STABLE (queue_id, user_id) string (H12), so a retry
    — whether Celery autoretry or the outbox beat sweep — re-sends the same
    identifier and Stripe dedupes server-side: retries never double-bill.

    Returns the MeterEvent identifier on success, or None when there is
    nothing billable / Stripe is intentionally off (a terminal no-op the
    caller stamps as reported). Raises on a transient Stripe failure.
    """
    if billable_units <= 0:
        return None

    if not _stripe_enabled():
        _logger.warning(
            "Stripe not fully configured — skipping meter event for user %s (%d billable units)",
            user_id[:8], billable_units,
        )
        raise _StripeNotConfiguredError("stripe_not_configured")

    if not stripe_customer_id:
        # No customer id is a terminal data condition, not a transient fault —
        # retrying cannot conjure a customer id. Treat as a reported no-op so
        # the outbox row stops being swept, but surface it loudly.
        _logger.error(
            "User %s has no stripe_customer_id — cannot bill %d skip-trace "
            "overage units (queue %d)",
            user_id[:8], billable_units, queue_id,
        )
        raise _StripeNotConfiguredError("no_customer_id")

    # H12 (full-SaaS review): the identifier must be STABLE across webhook
    # replays so Stripe's own dedup kicks in. Keyed on (queue_id, user_id)
    # so a Stripe-side retry of the same overage always dedupes.
    stable_identifier = f"skip_trace_q{queue_id}_u{user_id}"

    # Any Stripe/network exception propagates — the durable report task's
    # autoretry_for=(Exception,) retries it, and the stable identifier above
    # makes that retry idempotent. We do NOT swallow it here.
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    event = stripe.billing.MeterEvent.create(
        event_name=settings.STRIPE_METER_EVENT_NAME_SKIP_TRACE,
        payload={
            "value": str(billable_units),
            "stripe_customer_id": stripe_customer_id,
        },
        identifier=stable_identifier,
    )
    _logger.info(
        "Reported %d skip-trace lookups to Stripe for user %s (plan=%s, over-quota)",
        billable_units, user_id[:8], plan,
    )
    return event.get("identifier") or event.get("id")


def report_usage_from_webhook(db, queue_id: int) -> dict:
    """Aggregate per-user usage for a completed batch, advance each user's
    counter, and persist the billable MeterEvents to the outbox — all WITHOUT
    committing (the caller commits).

    Reads pending_skip_trace_rows for the given Tracerfy queue_id, groups by
    user_id, and calls report_lookups_for_user for each user. The counter
    advances land in the CALLER'S transaction (the ingest worker commits them
    alongside the SkipTraceQueue status flip, per REDTEAM B1).

    REDTEAM (Codex convergence — meter outbox): the deferred Stripe MeterEvent
    used to be returned as in-memory kwargs ("pending_meter_events") that the
    worker enqueued best-effort AFTER commit. If the broker was down at that
    instant the event was logged and lost, and because the queue was already
    "completed" there was no persisted record to recover from → permanent
    under-billing. Now, for every billable user, we INSERT a SkipTraceMeterEvent
    outbox row into the SAME db session, so the intent-to-bill commits
    atomically with the counter advance and queue-status flip. INSERT ... ON
    CONFLICT (tracerfy_queue_id, user_id) DO NOTHING makes a re-run idempotent
    on replay. We then query the outbox row ids back (within this txn) and
    return them so the caller can enqueue report_skip_trace_meter_event by id;
    any lost enqueue is recovered by the flush_skip_trace_meter_outbox beat
    task scanning reported_at IS NULL rows.

    The _stripe_enabled() gate stays OFF here: the counter must advance for
    usage tracking regardless of whether Stripe billing is wired up. The
    per-user Stripe call is gated inside report_meter_event_to_stripe().

    Returns a dict:
        queue_id: int
        users: per-user summary (used_after, billable_units, ...)
        outbox_ids: list of SkipTraceMeterEvent ids this batch owns, for the
            caller to enqueue after commit
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import SkipTraceMeterEvent

    # ORDER BY user_id (Codex review): report_lookups_for_user takes a
    # SELECT ... FOR UPDATE lock on each user row and the outbox change keeps it
    # held until the caller's single commit. Two concurrent webhooks whose
    # batches share users could otherwise lock them in opposite orders (U1→U2
    # vs U2→U1) and deadlock. Taking the locks in a deterministic ascending
    # user_id order across all batches makes a deadlock impossible.
    rows = db.execute(
        text("""
            SELECT user_id, COUNT(*) as n
            FROM pending_skip_trace_rows
            WHERE tracerfy_queue_id = :qid
              AND status = 'completed'
            GROUP BY user_id
            ORDER BY user_id
        """),
        {"qid": queue_id},
    ).fetchall()

    summary: dict = {"queue_id": queue_id, "users": [], "outbox_ids": []}
    for row in rows:
        result = report_lookups_for_user(
            db, str(row.user_id), row.n, queue_id=queue_id
        )
        summary["users"].append({
            "user_id_prefix": str(row.user_id)[:8],
            "n": row.n,
            **result,
        })
        if result.get("billable_units", 0) > 0:
            # Persist the billable MeterEvent to the outbox in THIS txn. ON
            # CONFLICT DO NOTHING on (tracerfy_queue_id, user_id) means a
            # replay can't duplicate the row; the existing reported_at on the
            # surviving row decides whether it still needs to be fired.
            db.execute(
                pg_insert(SkipTraceMeterEvent)
                .values(
                    tracerfy_queue_id=queue_id,
                    user_id=str(row.user_id),
                    billable_units=result["billable_units"],
                    stripe_customer_id=result.get("stripe_customer_id"),
                    plan=result.get("plan", ""),
                )
                .on_conflict_do_nothing(
                    index_elements=["tracerfy_queue_id", "user_id"]
                )
            )

    # Read back the outbox row ids for this batch within the same txn so the
    # caller can enqueue them after commit. Querying by queue_id (rather than
    # relying on INSERT RETURNING) also surfaces rows a prior partial run may
    # have already inserted, so a retry still recovers them.
    outbox_rows = db.execute(
        text("""
            SELECT id
            FROM skip_trace_meter_events
            WHERE tracerfy_queue_id = :qid
              AND reported_at IS NULL
        """),
        {"qid": queue_id},
    ).fetchall()
    summary["outbox_ids"] = [str(r.id) for r in outbox_rows]

    return summary
