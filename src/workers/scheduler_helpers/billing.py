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
    instantly at cap. So it now runs DAILY at 00:05 UTC and catches up.

    QUOTA-LOSS FIX: that catch-up design used a single blanket
    ``SET records_used = 0 WHERE records_period_start IS NULL OR
    records_period_start < this_month``, which destroyed usage TWO ways:

      1. ``records_period_start`` had no server_default and was never set
         at registration, so EVERY new user was NULL and got zeroed on
         their first 00:05 run — inside their own signup month, with no
         billing event. (Prod: a user billed 999 records on day 1 and
         woke up at 2.)
      2. Zeroing was unconditional, so when Beat DID miss the 1st — the
         very case this task exists for — the late run also wiped usage
         already billed inside the NEW period. (Prod: 67 records billed
         Sep 2 destroyed by a Sep 3 catch-up run.)

    Both are fixed by splitting the blanket UPDATE into two statements
    with different semantics, and by making billing itself roll the
    period forward atomically (see ``_bill_records_used`` in
    workers/tasks.py). Because billing advances records_period_start in
    the same statement that increments records_used, a period_start that
    is STILL stale here PROVES no job billed in the current period — so
    zeroing those rows is correct by construction, not by hope.

      * NULL period_start  -> ADOPT: stamp the period, keep the counter.
        Zeroing is the financially destructive direction (it grants free
        quota and lets a user exceed their cap invisibly), so an
        unexpected NULL must never cost us the counter. NULL should be
        unreachable after migration 086 + the registration fix; if one
        appears anyway it is a bug, so we alert on it.
      * STALE period_start -> ROLL OVER: zero the counter and advance.

    Skip-trace gets the SAME two-statement treatment, keyed on its OWN
    ``skip_trace_period_start``. It was previously gated on
    ``records_period_start``, so a drift between the two columns could
    reset skip-trace early or never reset it at all — skip-trace is
    metered to Stripe, so that is a billing-correctness bug in its own
    right.
    """
    from sqlalchemy import text

    from src.db.session import system_sync_session

    now = datetime.now(UTC)

    # NOTE: every statement below recomputes the boundary with the same
    # expression rather than sharing an interpolated constant, so the SQL stays
    # static (no string-built queries) and Postgres evaluates one consistent
    # NOW() per statement.

    with system_sync_session() as db:
        # ── 1. ADOPT rows with no period at all (stamp only, never zero) ──────
        adopted_records = db.execute(
            text("""
                UPDATE users
                SET records_period_start = date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                WHERE records_period_start IS NULL
            """)
        ).rowcount
        adopted_skip = db.execute(
            text("""
                UPDATE users
                SET skip_trace_period_start = date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                WHERE skip_trace_period_start IS NULL
            """)
        ).rowcount

        # ── 2. ROLL OVER genuinely stale periods (zero + advance) ─────────────
        rolled_records = db.execute(
            text("""
                UPDATE users
                SET records_used = 0,
                    records_period_start = date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                WHERE records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """)
        ).rowcount
        rolled_skip = db.execute(
            text("""
                UPDATE users
                SET skip_trace_used_this_month = 0,
                    skip_trace_period_start = date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                WHERE skip_trace_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """)
        ).rowcount
        db.commit()

    _logger.info(
        "Daily usage rollover at %s: rolled %d records / %d skip-trace periods; "
        "adopted %d records / %d skip-trace NULL periods",
        now.isoformat(), rolled_records, rolled_skip, adopted_records, adopted_skip,
    )

    # A NULL period_start is unreachable once migration 086 has run and
    # registration stamps both columns. If one shows up, an insert path is
    # bypassing that — surface it instead of silently absorbing it.
    if adopted_records or adopted_skip:
        try:
            from src.workers.ops_alerts import send_ops_alert

            send_ops_alert(
                kind="billing",
                key="null_billing_period",
                subject="Quota rollover adopted NULL billing periods",
                body=(
                    f"reset_monthly_usage stamped {adopted_records} NULL "
                    f"records_period_start and {adopted_skip} NULL "
                    f"skip_trace_period_start rows. Both columns are NOT NULL "
                    f"with a server_default as of migration 086, so this means "
                    f"an insert path is writing an explicit NULL. Counters were "
                    f"PRESERVED (not zeroed); investigate the insert path."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never break the beat
            _logger.warning("Could not send NULL-period ops alert: %s", exc)


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
