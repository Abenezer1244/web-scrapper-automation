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
    queue_id: int | None = None,
) -> dict:
    """Increment user counter and report over-quota units to Stripe.

    Args:
        db: SQLAlchemy session (must be in an active transaction)
        user_id: UUID string of the user whose rows were ingested
        new_lookups: Number of rows in the completed Tracerfy batch that
            were attributable to this user
        queue_id: Tracerfy queue_id for the batch. Used to build a
            stable Stripe MeterEvent identifier so webhook replay
            dedupes on Stripe's side. H12 from the full-SaaS review.

    Returns:
        Dict with keys:
            plan: the user's current plan
            quota: the bundled monthly quota for that plan
            used_before: counter value before this ingest
            used_after: counter value after this ingest
            billable_units: number of units reported to Stripe (0 if all bundled)
            meter_event_id: Stripe MeterEvent ID if reported, else None
            error: str if reporting failed (ingest still commits)
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

    # Persist the counter regardless of billing success
    db.execute(
        text("""
            UPDATE users
            SET skip_trace_used_this_month = :used
            WHERE id = :uid
        """),
        {"used": used_after, "uid": user_id},
    )

    # H11 (full-SaaS review): commit the counter + release the
    # SELECT FOR UPDATE row lock BEFORE calling Stripe. Previously
    # we held the lock for the duration of the Stripe API call
    # (~500ms per user), which serialized concurrent webhook
    # ingests for the same user and held DB connections open
    # during every skip-trace meter report. Committing here also
    # guarantees the counter increment survives even if Stripe is
    # down — the next cycle will still see used_this_month
    # advanced correctly. If the Stripe call fails we log an error
    # but do not raise, since the counter is already committed
    # and raising would prompt a meaningless rollback.
    db.commit()

    result = {
        "plan": plan,
        "quota": quota,
        "used_before": used_before,
        "used_after": used_after,
        "billable_units": billable_units,
        "meter_event_id": None,
    }

    if billable_units <= 0:
        _logger.info(
            "User %s used %d/%d bundled lookups (no overage this batch)",
            user_id[:8], used_after, quota,
        )
        return result

    if not _stripe_enabled():
        _logger.warning(
            "Stripe not fully configured — skipping meter event for user %s (%d billable units)",
            user_id[:8], billable_units,
        )
        result["error"] = "stripe_not_configured"
        return result

    if not user_row.stripe_customer_id:
        _logger.warning(
            "User %s has no stripe_customer_id — skipping meter event (%d billable)",
            user_id[:8], billable_units,
        )
        result["error"] = "no_customer_id"
        return result

    # Report to Stripe. H12 (full-SaaS review): the identifier must
    # be STABLE across webhook replays so Stripe's own dedup kicks in.
    # The old identifier included now.isoformat() which changed on
    # every retry, so a replayed Tracerfy webhook would create a
    # new MeterEvent and bill the customer twice. Use
    # (queue_id, user_id) when queue_id is provided (the webhook
    # path); fall back to a period-stable key only when called
    # without queue_id (no current caller, but safe default).
    if queue_id is not None:
        stable_identifier = f"skip_trace_q{queue_id}_u{user_id}"
    else:
        # Legacy fallback — stable within a billing period but not
        # across. No current caller hits this path.
        period_tag = (
            user_row.skip_trace_period_start.isoformat()
            if user_row.skip_trace_period_start else "no_period"
        )
        stable_identifier = f"skip_trace_legacy_{user_id}_{period_tag}"

    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        event = stripe.billing.MeterEvent.create(
            event_name=settings.STRIPE_METER_EVENT_NAME_SKIP_TRACE,
            payload={
                "value": str(billable_units),
                "stripe_customer_id": user_row.stripe_customer_id,
            },
            identifier=stable_identifier,
        )
        result["meter_event_id"] = event.get("identifier") or event.get("id")
        _logger.info(
            "Reported %d skip-trace lookups to Stripe for user %s (plan=%s, over-quota)",
            billable_units, user_id[:8], plan,
        )
    except Exception as exc:
        _logger.error(
            "Failed to report meter event for user %s: %s",
            user_id[:8], str(exc)[:200],
        )
        result["error"] = f"stripe_error: {str(exc)[:100]}"

    return result


def report_usage_from_webhook(db, queue_id: int) -> dict:
    """Aggregate per-user usage from a completed Tracerfy batch and report.

    Reads pending_skip_trace_rows for the given Tracerfy queue_id,
    groups by user_id, and calls report_lookups_for_user for each user.

    Returns a summary dict with per-user billable totals.
    """
    if not _stripe_enabled():
        _logger.debug("Stripe not enabled — skipping usage report for queue %d", queue_id)
        return {"skipped": "stripe_not_configured"}

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

    summary = {"queue_id": queue_id, "users": []}
    for row in rows:
        result = report_lookups_for_user(
            db, str(row.user_id), row.n, queue_id=queue_id
        )
        summary["users"].append({
            "user_id_prefix": str(row.user_id)[:8],
            "n": row.n,
            **result,
        })

    return summary
