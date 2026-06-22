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


def _expire_trials_impl() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users where trial_ends_at < now and plan is still 'pro'
    with no stripe_customer_id (paying users keep their plan).
    """
    from sqlalchemy import select

    from src.config import settings
    from src.db.models import User
    from src.db.session import SyncSessionLocal

    now = datetime.now(UTC)

    with SyncSessionLocal() as db:
        # Find trial users whose trial has expired and who haven't paid
        expired = db.execute(
            select(User).where(
                User.trial_ends_at.isnot(None),
                User.trial_ends_at < now,
                User.plan != "starter",
                User.stripe_customer_id.is_(None),  # Not a paying customer
            )
        ).scalars().all()

        for user in expired:
            user.plan = "starter"
            user.records_limit = settings.PLAN_LIMITS["starter"]  # post-trial Starter limit
            _logger.info("Trial expired for %s — downgraded to starter", user.email)
            from src.api.entitlements import apply_reconciliation_sync
            apply_reconciliation_sync(db, str(user.id), "starter")

        if expired:
            db.commit()
            _logger.info("Expired %d trials", len(expired))
        else:
            _logger.info("No expired trials to process")
