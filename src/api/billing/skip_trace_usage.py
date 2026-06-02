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


def report_meter_event_to_stripe(
    user_id: str,
    queue_id: int,
    billable_units: int,
    stripe_customer_id: str | None,
    plan: str,
) -> dict:
    """Phase 2 of REDTEAM B2: fire the Stripe MeterEvent AFTER the caller has
    committed the counter advance.

    Called once per user, after the worker's single ingest+billing+status
    transaction commits. Stripe failures are logged and returned (never
    raised): the counter is already durably advanced and the queue is marked
    billed, so a raise would only force a pointless retry of the whole ingest
    — which would now no-op on the B1 idempotency guard anyway.

    Returns a dict with meter_event_id (on success) or error (on skip/fail).
    """
    out: dict = {"meter_event_id": None}

    if billable_units <= 0:
        return out

    if not _stripe_enabled():
        _logger.warning(
            "Stripe not fully configured — skipping meter event for user %s (%d billable units)",
            user_id[:8], billable_units,
        )
        out["error"] = "stripe_not_configured"
        return out

    if not stripe_customer_id:
        _logger.warning(
            "User %s has no stripe_customer_id — skipping meter event (%d billable)",
            user_id[:8], billable_units,
        )
        out["error"] = "no_customer_id"
        return out

    # H12 (full-SaaS review): the identifier must be STABLE across webhook
    # replays so Stripe's own dedup kicks in. Keyed on (queue_id, user_id)
    # so a Stripe-side retry of the same overage always dedupes.
    stable_identifier = f"skip_trace_q{queue_id}_u{user_id}"

    try:
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
        out["meter_event_id"] = event.get("identifier") or event.get("id")
        _logger.info(
            "Reported %d skip-trace lookups to Stripe for user %s (plan=%s, over-quota)",
            billable_units, user_id[:8], plan,
        )
    except Exception as exc:
        _logger.error(
            "Failed to report meter event for user %s: %s",
            user_id[:8], str(exc)[:200],
        )
        out["error"] = f"stripe_error: {str(exc)[:100]}"

    return out


def report_usage_from_webhook(db, queue_id: int) -> dict:
    """Phase 1 of REDTEAM B2: aggregate per-user usage for a completed batch
    and advance each user's counter WITHOUT committing.

    Reads pending_skip_trace_rows for the given Tracerfy queue_id, groups by
    user_id, and calls report_lookups_for_user for each user. The counter
    advances land in the CALLER'S transaction (the ingest worker commits them
    alongside the SkipTraceQueue status flip, per REDTEAM B1). The deferred
    Stripe MeterEvent calls are returned in "pending_meter_events" so the
    caller can fire them via report_meter_event_to_stripe() AFTER it commits.

    REDTEAM B2: the old version gated the whole function on _stripe_enabled()
    and returned early when Stripe was off — but the counter still needs to
    advance for usage tracking regardless of whether Stripe billing is wired
    up, so the gate is removed here. The per-user Stripe call is gated inside
    report_meter_event_to_stripe() instead.

    Returns a dict:
        queue_id: int
        users: per-user summary (used_after, billable_units, ...)
        pending_meter_events: list of kwargs for report_meter_event_to_stripe
    """
    rows = db.execute(
        text("""
            SELECT user_id, COUNT(*) as n
            FROM pending_skip_trace_rows
            WHERE tracerfy_queue_id = :qid
              AND status = 'completed'
            GROUP BY user_id
        """),
        {"qid": queue_id},
    ).fetchall()

    summary: dict = {"queue_id": queue_id, "users": [], "pending_meter_events": []}
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
            summary["pending_meter_events"].append({
                "user_id": str(row.user_id),
                "queue_id": queue_id,
                "billable_units": result["billable_units"],
                "stripe_customer_id": result.get("stripe_customer_id"),
                "plan": result.get("plan", ""),
            })

    return summary
