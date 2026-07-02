"""Body logic for the billing beat tasks: reset_monthly_usage + expire_trials."""

from datetime import UTC, datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _reset_monthly_usage_impl() -> None:
    """Roll over monthly usage counters when the billing period ends.

    H5 (full-SaaS review): previously this ran on a crontab at
    day_of_month=1, hour=0, minute=0. Celery Beat does NOT backfill
    missed cron ticks — if Beat was down at that instant (Railway
    redeploy, broker hiccup) the reset was SKIPPED ENTIRELY and every
    user carried last month's records_used forward into the new
    month. A user at 500/500 last month started the new month
    instantly at cap.

    Now runs daily at 00:05 UTC. On each run, finds users whose
    records_period_start points at a month strictly earlier than
    this run's current month and resets their counters +
    advances records_period_start to the first of the current
    month. Idempotent: a user who was already reset this month
    has records_period_start = this month and is skipped.

    The same logic applies to skip_trace_used_this_month +
    skip_trace_period_start for Sprint 4 billing.
    """
    from sqlalchemy import text

    from src.db.session import system_sync_session

    now = datetime.now(UTC)

    with system_sync_session() as db:
        # Truncate to first-of-current-month at UTC. Postgres
        # date_trunc('month', ...) gives us the boundary.
        result = db.execute(
            text("""
                WITH period_start AS (
                    SELECT date_trunc('month', NOW() AT TIME ZONE 'UTC')
                           AT TIME ZONE 'UTC' AS this_month
                )
                UPDATE users
                SET
                    records_used = 0,
                    records_period_start = (SELECT this_month FROM period_start),
                    skip_trace_used_this_month = 0,
                    skip_trace_period_start = (SELECT this_month FROM period_start)
                WHERE
                    records_period_start IS NULL
                    OR records_period_start
                       < (SELECT this_month FROM period_start)
            """)
        )
        db.commit()
        _logger.info(
            "Daily usage rollover: reset %d users whose period_start "
            "was earlier than %s",
            result.rowcount, now.isoformat(),
        )


#: Stripe subscription statuses that GRANT entitlement — a user in any of these
#: is paying (or in a Stripe-side trial / dunning grace) and must NOT have their
#: app-side trial expired out from under them. Everything else (canceled, unpaid,
#: incomplete, incomplete_expired, paused, or NULL) is treated as not entitled.
_ENTITLED_SUB_STATUSES = ("active", "trialing", "past_due")


def _stripe_entitled_status(customer_id: str) -> str | None:
    """Return the customer's current ENTITLED Stripe subscription status
    (active/trialing/past_due), or None if they have no entitled subscription.

    Raises on Stripe/transport errors — the caller treats that as 'unknown' and
    does NOT downgrade (never downgrade a possible payer on a transient failure)."""
    import stripe

    from src.config import settings

    stripe.api_key = settings.STRIPE_SECRET_KEY
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    for s in subs.get("data", []):
        if s.get("status") in _ENTITLED_SUB_STATUSES:
            return s.get("status")
    return None


def _expire_trials_impl(subscription_lookup=None) -> None:
    """Downgrade expired-trial users from Pro to Starter — unless they are paying.

    Runs hourly. `stripe_customer_id` is NOT an entitlement signal (it is created
    when a user merely OPENS checkout), so the gate uses the durable subscription
    state (migration 077). Per-row decision:
      * known-entitled status (active/trialing/past_due) -> excluded by the query.
      * known non-entitled status (e.g. canceled)        -> downgrade.
      * no stripe_customer_id (never reached Stripe)      -> downgrade.
      * AMBIGUOUS (customer id present, status NULL — a legacy payer OR an
        abandoned checkout) -> ask Stripe (the source of truth): entitled ->
        protect + record the status (self-heal); not entitled -> downgrade +
        record; Stripe error -> SKIP (never downgrade a possible payer).

    This resolves BOTH Codex P1s: legacy payers are never wrongly downgraded, and
    future abandoned-checkout trials DO expire — automatically, no manual backfill.
    `subscription_lookup` is injectable for tests (default = live Stripe).
    """
    from sqlalchemy import or_, select

    from src.api.entitlements import apply_reconciliation_sync
    from src.config import settings
    from src.db.models import User
    from src.db.session import SyncSessionLocal

    lookup = subscription_lookup or _stripe_entitled_status
    now = datetime.now(UTC)

    def _downgrade(db, user) -> None:
        user.plan = "starter"
        user.records_limit = settings.PLAN_LIMITS["starter"]  # post-trial Starter limit
        _logger.info("Trial expired for %s — downgraded to starter", user.email)
        apply_reconciliation_sync(db, str(user.id), "starter")

    with SyncSessionLocal() as db:
        # Candidates: expired trial, still on a paid tier, NOT known-entitled.
        candidates = db.execute(
            select(User).where(
                User.trial_ends_at.isnot(None),
                User.trial_ends_at < now,
                User.plan != "starter",
                or_(
                    User.subscription_status.is_(None),
                    User.subscription_status.notin_(_ENTITLED_SUB_STATUSES),
                ),
            )
        ).scalars().all()

        downgraded = 0
        for user in candidates:
            if user.subscription_status is not None:
                _downgrade(db, user)  # known non-entitled (canceled/unpaid/…)
                downgraded += 1
            elif not user.stripe_customer_id:
                _downgrade(db, user)  # never reached Stripe -> genuine unpaid trial
                downgraded += 1
            else:
                # AMBIGUOUS: customer id present, status NULL. Stripe is the truth.
                try:
                    status = lookup(user.stripe_customer_id)
                except Exception as exc:  # noqa: BLE001 - any failure = unknown
                    _logger.warning(
                        "expire_trials: Stripe lookup failed for user %s (customer "
                        "%s) — skipping, NOT downgrading: %s",
                        user.id, user.stripe_customer_id, exc,
                    )
                    continue
                if status in _ENTITLED_SUB_STATUSES:
                    user.subscription_status = status  # legacy payer: protect + self-heal
                    _logger.info(
                        "expire_trials: user %s has entitled Stripe status %s — "
                        "protected + backfilled", user.id, status,
                    )
                else:
                    user.subscription_status = "canceled"  # record non-entitlement
                    _downgrade(db, user)
                    downgraded += 1

        if candidates:
            db.commit()
        _logger.info(
            "expire_trials: %d candidates, %d downgraded", len(candidates), downgraded
        )
