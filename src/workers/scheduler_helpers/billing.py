"""Body logic for the billing beat tasks: skip-trace rollover, quota
reconciliation, and trial expiry."""

from datetime import UTC, datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _reset_skip_trace_usage_impl() -> None:
    """Roll over the SKIP-TRACE counter on the calendar month.

    This task used to reset record quota too. It no longer does, and that is the
    whole point of migration 088: record quota is metered over each user's own
    ENTITLEMENT WINDOW (``users.quota_period_start`` / ``quota_period_end``),
    advanced by ``reconcile_quota_periods`` and by the lazy rollover inside the
    statements that charge. Leaving the old blanket

        UPDATE users SET records_used = 0
        WHERE records_period_start < date_trunc('month', now())

    in place alongside anchored windows would be a DOUBLE GRANT: a subscriber
    anchored on the 20th would be zeroed by their own boundary on the 20th and
    again by this task on the 1st. Retiring the records half in the same deploy
    that introduces windows is what guarantees exactly one mechanism owns the
    counter at every instant — never two, and never none.

    Skip-trace stays calendar-metered on purpose. It is billed to Stripe on its
    own meter against its own ``skip_trace_period_start`` column, was never part
    of the entitlement-window decision, and moving it is a separate change with
    its own billing consequences.

    The two-statement shape is kept verbatim from the records version, because
    the reasoning behind it is unchanged and was learned expensively:

      * NULL period_start  -> ADOPT: stamp the period, keep the counter. Zeroing
        is the financially destructive direction, so an unexpected NULL must
        never cost us the counter. NULL is unreachable after migration 086; if
        one appears, an insert path is writing an explicit NULL, so we alert.
      * STALE period_start -> ROLL OVER: zero the counter and advance.

    Runs daily rather than on a cron at the 1st: Celery Beat does not backfill
    missed ticks, so a redeploy at that instant would skip a whole month.
    """
    from sqlalchemy import text

    from src.db.session import system_sync_session

    now = datetime.now(UTC)

    # NOTE: each statement recomputes the boundary with the same expression
    # rather than sharing an interpolated constant, so the SQL stays static (no
    # string-built queries) and Postgres evaluates one consistent NOW() per
    # statement.
    with system_sync_session() as db:
        adopted_skip = db.execute(
            text("""
                UPDATE users
                SET skip_trace_period_start = date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                WHERE skip_trace_period_start IS NULL
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
        "Daily skip-trace rollover at %s: rolled %d period(s); adopted %d NULL period(s)",
        now.isoformat(), rolled_skip, adopted_skip,
    )

    if adopted_skip:
        try:
            from src.workers.ops_alerts import send_ops_alert

            send_ops_alert(
                kind="billing",
                key="null_billing_period",
                subject="Skip-trace rollover adopted NULL billing periods",
                body=(
                    f"reset_skip_trace_usage stamped {adopted_skip} NULL "
                    f"skip_trace_period_start rows. The column is NOT NULL with a "
                    f"server_default as of migration 086, so this means an insert "
                    f"path is writing an explicit NULL. The counter was PRESERVED "
                    f"(not zeroed); investigate the insert path."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never break the beat
            _logger.warning("Could not send NULL-period ops alert: %s", exc)


def _reconcile_quota_periods_impl(subscription_lookup=None) -> dict[str, int]:
    """Advance entitlement windows that have ended — RECONCILIATION, not the
    source of correctness.

    Quota correctness does not depend on this task, on Stripe webhooks, on cron
    or on the user logging in. Every authoritative quota operation (reserve and
    settle) advances the window itself, atomically, inside the statement that
    charges. This exists for the users those paths never touch: someone who runs
    no scrape for two months still needs a true window when they come back, and
    ``/billing/usage`` should not be the only thing that knows it.

    It is therefore idempotent, tolerates missed runs, and cannot grant a
    duplicate bucket:

      * ``quota_should_roll`` is re-evaluated under the row lock, so a window a
        worker has just advanced no longer qualifies;
      * ``SKIP LOCKED`` steps over any user currently being charged rather than
        blocking behind them — that worker is performing the same rollover, and
        anyone genuinely missed is picked up on the next run;
      * a user away for months lands on the window containing NOW and is zeroed
        ONCE, so unused entitlement never accumulates;
      * a frozen (unpaid, or past_due beyond its grace) subscription is excluded
        by the predicate, so failing to pay cannot mint a fresh bucket monthly.

    It also repairs two states a webhook may have failed to deliver — which is
    the difference between reconciliation and a second quota system:

      * a paid entitlement whose end has PASSED with no ``subscription.deleted``
        ever arriving. The user is downgraded to Starter here, which also
        RELEASES the window (``entitlement_ends_at`` was what held it), so one
        lost webhook cannot strand someone frozen forever. Stripe is CONSULTED
        first, because the reverse webhook can go missing too: a customer who
        scheduled a cancellation and then reversed it would otherwise be
        downgraded on the old end date and could later "resubscribe" into
        another fresh window. Same doctrine as ``_expire_trials_impl`` — a
        Stripe error means UNKNOWN, and we never downgrade a possible payer on a
        transient failure;
      * configs still active under a plan that has since been downgraded.

    Returns a small summary so the beat task can log and tests can assert on it.
    """
    from sqlalchemy import text

    from src.api.entitlements import apply_reconciliation_sync
    from src.api.quota_window import window_cte_sql, window_set_sql
    from src.config import settings
    from src.db.session import system_sync_session

    lookup = subscription_lookup or _stripe_entitled_status
    now = datetime.now(UTC)
    changed_plan: set[str] = set()

    with system_sync_session() as db:
        # ── 1. Expired paid entitlement with no subscription.deleted ─────────
        # Runs BEFORE the rollover on purpose: clearing entitlement_ends_at is
        # exactly what makes such a window eligible to advance in the same pass.
        # Read the candidates and END the read transaction before touching
        # Stripe. Each lookup is a network call, and holding a transaction open
        # across N of them would keep every row this loop has already written
        # locked for the duration — long enough to block a worker's reserve or
        # settle on those same users. Each decision below commits on its own, so
        # one slow or failing account cannot hold up the rest. Bounded, because
        # an unbounded beat pass making unbounded Stripe calls is its own outage.
        candidates = db.execute(
            text(
                "SELECT id, stripe_subscription_id, stripe_customer_id "
                "FROM users "
                "WHERE entitlement_ends_at IS NOT NULL "
                "  AND entitlement_ends_at <= CAST(:at AS timestamptz) "
                "ORDER BY entitlement_ends_at "
                "LIMIT :lim"
            ),
            {"at": now, "lim": _RECONCILE_LAPSED_LIMIT},
        ).fetchall()
        db.commit()

        lapsed: list[str] = []
        for row in candidates:
            user_id, _sub_id, customer_id = str(row[0]), row[1], row[2]
            if customer_id:
                # Ask Stripe before taking anything away. A cancellation the
                # customer REVERSED, whose update webhook was lost, must not be
                # honoured on the stale end date.
                try:
                    status = lookup(customer_id)
                except Exception as exc:  # noqa: BLE001 — any failure = unknown
                    _logger.warning(
                        "reconcile: Stripe lookup failed for user %s (customer "
                        "%s) — skipping, NOT downgrading: %s",
                        user_id, customer_id, str(exc)[:200],
                    )
                    continue
                if status in _ENTITLED_SUB_STATUSES:
                    # Still paying: the cancellation was reversed. Clear the end
                    # date so their window can advance again, and self-heal the
                    # status we had drifted from.
                    db.execute(
                        text(
                            "UPDATE users SET entitlement_ends_at = NULL, "
                            "subscription_status = :st "
                            "WHERE id = CAST(:uid AS uuid)"
                        ),
                        {"uid": user_id, "st": status},
                    )
                    db.commit()
                    _logger.info(
                        "reconcile: user %s is still entitled in Stripe (%s) — "
                        "cancellation reversed, entitlement end cleared",
                        user_id, status,
                    )
                    continue
            db.execute(
                text(
                    "UPDATE users SET "
                    "  plan = 'starter', "
                    "  records_limit = :starter_limit, "
                    "  stripe_subscription_id = NULL, "
                    "  subscription_status = 'canceled', "
                    "  paid_entitlement_ended_at = COALESCE(paid_entitlement_ended_at, "
                    "                                       entitlement_ends_at), "
                    "  entitlement_ends_at = NULL, "
                    "  pending_plan = NULL, "
                    "  pending_records_limit = NULL "
                    "WHERE id = CAST(:uid AS uuid)"
                ),
                {"uid": user_id, "starter_limit": settings.PLAN_LIMITS["starter"]},
            )
            db.commit()
            lapsed.append(user_id)
        changed_plan.update(lapsed)

        # ── 2. Advance every window that has ended and may advance ───────────
        # records_used = w.base rather than a literal 0: base IS 0 for every row
        # this statement can reach (the predicate guarantees it), and writing it
        # through the shared projection keeps this site from being the one that
        # quietly disagrees with the others.
        rolled = db.execute(
            text(
                "WITH cur AS ("
                "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
                "         u.quota_period_start, u.quota_period_end,"
                "         u.subscription_status, u.entitlement_grace_ends_at,"
                "         u.entitlement_ends_at, u.pending_plan,"
                "         u.pending_records_limit"
                "  FROM users u"
                "  WHERE public.quota_should_roll(u.quota_period_end,"
                "        u.subscription_status, u.entitlement_grace_ends_at,"
                "        u.entitlement_ends_at, CAST(:at AS timestamptz))"
                "  ORDER BY u.id"
                "  FOR UPDATE SKIP LOCKED"
                "), w AS ("
                "  SELECT cur.*, " + window_cte_sql("", ":at") + " FROM cur"
                ") UPDATE users u SET"
                "    records_used = w.base,"
                + window_set_sql("w")
                + "  FROM w WHERE u.id = w.id"
                "  RETURNING u.id, w.pending_plan"
            ),
            {"at": now},
        ).fetchall()
        changed_plan.update(str(r[0]) for r in rolled if r[1] is not None)

        db.commit()

        # ── 3. Bring scraper configs back in line with any plan that moved ───
        # Deliberately after the commit: apply_reconciliation_sync issues its own
        # SELECT plus per-config UPDATEs and must not run inside the locked
        # window above. A no-op unless ENTITLEMENT_ENFORCEMENT is on.
        for user_id in changed_plan:
            plan = db.execute(
                text("SELECT plan FROM users WHERE id = CAST(:uid AS uuid)"),
                {"uid": user_id},
            ).scalar()
            if plan:
                apply_reconciliation_sync(db, user_id, plan)
        if changed_plan:
            db.commit()

    _logger.info(
        "Quota reconciliation at %s: rolled %d window(s), retired %d lapsed "
        "entitlement(s), reconciled configs for %d user(s)",
        now.isoformat(), len(rolled), len(lapsed), len(changed_plan),
    )
    return {
        "rolled": len(rolled),
        "lapsed": len(lapsed),
        "plans_reconciled": len(changed_plan),
    }


#: Stripe subscription statuses that GRANT entitlement — a user in any of these
#: is paying (or in a Stripe-side trial / dunning grace) and must NOT have their
#: app-side trial expired out from under them. Everything else (canceled, unpaid,
#: incomplete, incomplete_expired, paused, or NULL) is treated as not entitled.
_ENTITLED_SUB_STATUSES = ("active", "trialing", "past_due")

#: How many lapsed entitlements one reconciliation pass will resolve. Each one
#: costs a Stripe round trip, so an unbounded pass would turn a backlog into a
#: beat task that never finishes. The remainder is picked up next hour; nothing
#: is lost, because the candidates query is a state test, not a queue.
_RECONCILE_LAPSED_LIMIT = 200


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
      * no stripe_customer_id (never reached Stripe)      -> downgrade.
      * ANY stripe_customer_id -> ask Stripe (the source of truth): entitled ->
        protect, self-heal the status, and stamp first_paid_at / trial_consumed_at;
        not entitled -> downgrade + record; Stripe error -> SKIP (never downgrade
        a possible payer).

    A locally-recorded "canceled" is NOT taken at face value any more. Our copy
    of the status is a cache and can be wrong — a mis-ordered webhook, a
    cancellation the customer reversed — and acting on it unverified takes a plan
    away from someone who is paying for it. (Codex)

    This resolves BOTH Codex P1s: legacy payers are never wrongly downgraded, and
    future abandoned-checkout trials DO expire — automatically, no manual backfill.
    `subscription_lookup` is injectable for tests (default = live Stripe).

    ENTITLEMENT WINDOWS (migration 088): the downgrade is applied IMMEDIATELY
    rather than deferred to the next boundary like a paid downgrade, because an
    expired trial is not something the customer paid for — deferring would hand
    them another month of Pro quota for free. The window itself needs no special
    handling: a trial user's window ENDS at ``trial_ends_at``, so it is already
    eligible to roll and the next charging statement (or the reconciliation)
    advances it to a post-trial window at the Starter limit set here.

    ``trial_consumed_at`` is stamped permanently. It is the anti-farming control
    for trial -> paid -> cancel -> trial-again: ``trial_ends_at`` is CLEARED on
    conversion, so it cannot answer "did this account ever trial".
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
        if user.trial_consumed_at is None:
            user.trial_consumed_at = user.trial_ends_at or now
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
            if not user.stripe_customer_id:
                _downgrade(db, user)  # never reached Stripe -> genuine unpaid trial
                downgraded += 1
            else:
                # ANY customer id, whatever we think the status is: ask Stripe.
                #
                # This used to trust a locally-recorded non-entitled status
                # ("canceled", "unpaid") and downgrade without checking. Our copy
                # of that status can be wrong — a webhook we mis-ordered, a
                # reversal we never received — and downgrading a customer Stripe
                # says is ACTIVE takes away a plan they are paying for. Stripe is
                # the source of truth for entitlement; the local column is a
                # cache. Verifying costs one API call on a row that is, by
                # definition, already exceptional. (Codex)
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
                    # Stamp first_paid_at too. Without it this legacy payer keeps
                    # a NULL first_paid_at, and their next
                    # customer.subscription.updated would read as a FIRST
                    # conversion — zeroing the counter and handing an
                    # already-paying customer a free window. The migration
                    # cannot reach these rows (it has no Stripe access and their
                    # local status was NULL), so this is where they get healed.
                    # (Codex)
                    if user.first_paid_at is None:
                        user.first_paid_at = user.created_at or now
                    if user.trial_consumed_at is None:
                        user.trial_consumed_at = user.trial_ends_at or now
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
