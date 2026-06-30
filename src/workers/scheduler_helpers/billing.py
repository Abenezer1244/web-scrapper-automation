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


def _expire_trials_impl() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users whose app-side trial has expired and who do NOT have
    an entitled Stripe subscription. The gate keys on the durable subscription
    state (migration 077), NOT on stripe_customer_id: a customer id is created
    when a user merely OPENS checkout, so the old `stripe_customer_id IS NULL`
    gate let a trial user who never paid keep Pro forever. A real subscriber
    (active/trialing/past_due) is protected.

    SAFETY (Codex P1): an AMBIGUOUS legacy row — has a `stripe_customer_id` but a
    NULL `subscription_status` (e.g. paid BEFORE migration 077 and not yet touched
    by a webhook/backfill) — is NEVER downgraded here. We downgrade only with
    POSITIVE non-payment evidence: no customer id at all (never reached Stripe), or
    a known non-entitled status. This makes the gate safe regardless of deploy
    order; ambiguous rows are resolved authoritatively by the Stripe backfill
    (scripts/backfill_subscription_status.py), never by guessing. Wrongly
    downgrading a paying customer is worse than briefly retaining one freeloader.
    """
    from sqlalchemy import or_, select

    from src.config import settings
    from src.db.models import User
    from src.db.session import SyncSessionLocal

    now = datetime.now(UTC)

    with SyncSessionLocal() as db:
        expired = db.execute(
            select(User).where(
                User.trial_ends_at.isnot(None),
                User.trial_ends_at < now,
                User.plan != "starter",
                # (A) NOT entitled — no active/trialing/past_due subscription.
                or_(
                    User.subscription_status.is_(None),
                    User.subscription_status.notin_(_ENTITLED_SUB_STATUSES),
                ),
                # (B) POSITIVE non-payment evidence — never expire an ambiguous
                # legacy row (customer id present but status still NULL).
                or_(
                    User.stripe_customer_id.is_(None),
                    User.subscription_status.isnot(None),
                ),
            )
        ).scalars().all()

        from src.api.entitlements import apply_reconciliation_sync
        for user in expired:
            user.plan = "starter"
            user.records_limit = settings.PLAN_LIMITS["starter"]  # post-trial Starter limit
            _logger.info("Trial expired for %s — downgraded to starter", user.email)
            apply_reconciliation_sync(db, str(user.id), "starter")

        if expired:
            db.commit()
            _logger.info("Expired %d trials", len(expired))
        else:
            _logger.info("No expired trials to process")
