"""Skip-trace metered billing: report usage to Stripe after successful ingest.

Called by the Tracerfy webhook receiver after a batch's rows are ingested.
For each user whose records were in the batch:

  1. Count the ingested rows attributable to that user
  2. Determine the bundled quota for the user's plan (250 for pro,
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

from src.api.quota_window import effective_window
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
                   skip_trace_period_start, quota_period_start, quota_period_end,
                   quota_anchor_at, subscription_status, entitlement_grace_ends_at,
                   entitlement_ends_at
            FROM users
            WHERE id = :uid
            FOR UPDATE
        """),
        {"uid": user_id},
    ).fetchone()
    if not user_row:
        _logger.warning("report_lookups_for_user: user %s not found", user_id)
        return {"billable_units": 0, "meter_event_id": None, "error": "user_not_found"}

    from src.config.constants import normalize_plan

    plan = normalize_plan(user_row.plan)
    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS.get(plan, 0)
    used_before = user_row.skip_trace_used_this_month or 0
    used_after = used_before + new_lookups

    # Reset the counter when the user's ENTITLEMENT WINDOW has rolled.
    #
    # This used to key off the calendar month, which put the free allowance and
    # the billed overage on two different clocks. Records reset on the
    # subscriber's own anniversary (migration 088) and a Stripe metered
    # subscription item bills usage over the SUBSCRIPTION period, so a customer
    # who signed up on the 20th had their 250 free lookups reset on the 1st,
    # halfway through the period Stripe was invoicing. That is two free
    # allowances inside one paid month, and neither number matches the invoice.
    #
    # `skip_trace_period_start` now holds the START OF THE ENTITLEMENT WINDOW the
    # counter belongs to. Same column, new meaning, no migration: the value is
    # only ever compared against `quota_period_start` and stamped from it.
    #
    # The comparison is strictly less-than, so this can only ever RESET a
    # counter, never resurrect a spent one.
    #
    # It compares against the EFFECTIVE window, not the stored one. The stored `quota_period_start`
    # only advances when the lazy rollover or the hourly reconciliation gets to
    # it, so between a window ending and that catching up it still names the OLD
    # window. Comparing against it there says "no roll", leaves an exhausted
    # counter in place, and bills the customer for lookups that belong to the new
    # window's free allowance. Charging someone for something they were owed for
    # free is the one direction that must not happen, and it is exactly what
    # `effective_records_used` already avoids on the records side by asking the
    # same question through the same helper. Codex found this.
    now = datetime.now(UTC)
    period_start = user_row.skip_trace_period_start
    window_start, _window_end = effective_window(user_row, now)
    rolled = period_start is None or (
        window_start is not None and period_start < window_start
    )
    if rolled:
        _logger.info(
            "Skip-trace allowance rolled for user %s: window starts %s",
            user_id[:8], window_start,
        )
        used_before = 0
        used_after = new_lookups
        db.execute(
            text("UPDATE users SET skip_trace_period_start = :start WHERE id = :uid"),
            {"start": window_start or now, "uid": user_id},
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


class _MissingCustomerError(Exception):
    """The user has no Stripe customer id YET, so this event cannot be sent.

    Distinct from _StripeNotConfiguredError on purpose. "Stripe is off" is
    terminal and the outbox row should stop being swept; "no customer id" is a
    state the user leaves the moment they check out, and the usage is real and
    billable. Collapsing the two is how a billable event got written off.
    """


class _StripeNotConfiguredError(Exception):
    """Stripe billing is not wired up — a non-retryable skip, not a failure.

    REDTEAM (Codex convergence — meter outbox): distinguished from transient
    Stripe/network errors so the durable report path can treat "Stripe off"
    as a terminal no-op (stamp reported_at, stop) while still RAISING on real
    transient failures so Celery autoretry kicks in.
    """




# ─── Is this usage billable at all? ───────────────────────────────────────────

# Stripe refuses a meter event timestamped more than 35 calendar days back. A
# day of slack, because the check and the API call are not simultaneous and a
# retry can sit in the queue.
_BACKDATE_LIMIT_DAYS = 34


class _NotBillableError(Exception):
    """This usage must not be reported. `reason` says which rule refused it.

    Carries the disposition reason rather than a message, because the caller
    writes it to the row: the answer to "why was this never billed" has to
    survive in the database, not only in a log line nobody reads.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def _configured_metered_price_ids() -> set[str]:
    """Every skip-trace metered Price this deployment knows about."""
    return {
        p for p in (
            settings.STRIPE_PRICE_SKIP_TRACE_PRO,
            settings.STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL,
            settings.STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE,
            settings.STRIPE_PRICE_SKIP_TRACE_BUSINESS_ANNUAL,
            settings.STRIPE_PRICE_SKIP_TRACE_AGENCY_OVERAGE,
            settings.STRIPE_PRICE_SKIP_TRACE_AGENCY_ANNUAL,
        ) if p and p.startswith("price_")
    }


def assert_billable(stripe_customer_id: str | None, usage_at, now=None) -> dict:
    """Refuse unless a real billing agreement covered this usage. Returns the sub.

    THE P1. The sweep used to release a held row as soon as the owner had a
    `stripe_customer_id` — an id `create_checkout` writes when the checkout
    SESSION is created, before payment and before any subscription exists. Both
    outcomes of that are wrong, and which one you get is a race:

      * fired before a subscription exists -> the event has a timestamp inside
        no billable period, we stamp the row settled, and real usage is silently
        stranded;
      * a subscription starts first -> the whole backlog is charged
        retroactively at a rate the customer never agreed to.

    "The customer has a Stripe id" is not a billing agreement. What is: a LIVE
    subscription, carrying the metered skip-trace item, whose current period
    actually contains the moment the usage happened. All three matter —

      * `active` only. `trialing` is not permission to bill trial usage, and
        `past_due` / `unpaid` mean the last invoice did not clear, so adding
        more to it is not the right move without a human.
      * the metered ITEM must be on it. A subscription proves they bought a
        plan; only the metered item proves they bought per-lookup overage at a
        price. This is the fact the "checkout disclosed it" argument rests on,
        so it is checked rather than assumed.
      * the period must CONTAIN usage_at. Usage from before this subscription
        began does not become billable because a later one exists — that is the
        retroactive charge, just arrived at politely.

    Anything this refuses is kept, not written off: the caller records the
    reason and a human decides. Refusing to bill is reversible; billing someone
    for something they never agreed to is not.
    """
    from datetime import timedelta

    now = now or datetime.now(UTC)

    if usage_at is None:
        # Rows written before migration 092. Their real usage time is not
        # recoverable, and a guess here is a guess that gets charged for.
        raise _NotBillableError("usage_at_unknown")

    if usage_at.tzinfo is None:
        usage_at = usage_at.replace(tzinfo=UTC)

    if usage_at > now + timedelta(minutes=5):
        # Stripe rejects anything more than five minutes ahead; a timestamp from
        # the future means a clock problem, not usage.
        raise _NotBillableError("usage_at_in_future")

    if usage_at < now - timedelta(days=_BACKDATE_LIMIT_DAYS):
        # Too old for Stripe to accept against its true date. The alternative is
        # to send it with today's date, which is exactly the retroactive charge
        # this function exists to prevent, so it goes to a human instead.
        raise _NotBillableError("timestamp_expired")

    if not stripe_customer_id:
        raise _NotBillableError("no_customer_id")

    metered = _configured_metered_price_ids()
    if not metered:
        # Nothing to check against. Reporting anyway would bill against whatever
        # item Stripe happens to match.
        raise _NotBillableError("no_metered_price_configured")

    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        # status="all": the ACTIVE ones decide billability, but a customer who
        # has only CANCELED subscriptions has still had an agreement, and
        # writing their usage off as "never subscribed" would be wrong. The
        # default listing omits canceled, so this must be explicit.
        subs = list(
            stripe.Subscription.list(
                customer=stripe_customer_id, status="all", limit=100,
            ).auto_paging_iter()
        )
    except Exception:  # noqa: BLE001 — unknown is NOT "not billable"
        # Deliberately NOT a _NotBillableError: we could not ask. Propagating lets
        # the task's autoretry try again rather than settling the row on the
        # strength of an outage.
        raise

    ts = int(usage_at.timestamp())
    covered_but_closed = False

    for sub in subs:
        if sub.get("status") != "active":
            continue

        # The metered ITEM must have existed when the usage happened. Its
        # presence today says the customer agreed to a per-lookup price NOW; it
        # says nothing about a week ago. Adding the item on the 8th does not
        # retroactively price usage from the 7th, and billing it would be the
        # retroactive charge wearing a different hat (Codex).
        items = ((sub.get("items") or {}).get("data")) or []
        if not any(
            ((i or {}).get("price") or {}).get("id") in metered
            and (i.get("created") is None or i["created"] <= ts)
            for i in items
        ):
            continue

        start, end = sub.get("current_period_start"), sub.get("current_period_end")
        if start is None or end is None:
            continue
        if start <= ts < end:
            return sub

        # The usage predates this period but the subscription itself already
        # existed and carried the item: it was covered by an agreement, in a
        # period that has since closed. A renewal between the lookup and this
        # report is enough — usage at 23:59, renewal at 00:00, reported at
        # 00:01. Calling that "no agreement" and settling it non_billable
        # silently discards revenue the customer genuinely owes, which is the
        # stranding failure this whole change exists to stop, just narrower.
        started = sub.get("start_date")
        if started is not None and started <= ts < start:
            covered_but_closed = True

    if covered_but_closed:
        raise _NotBillableError("closed_billing_period")

    # A customer who has never had ANY subscription cannot have had an
    # agreement covering this usage, whatever its timestamp. That is the one
    # refusal we can make with certainty, and it is the case the owner approved
    # writing off. Everything else — a cancelled subscription, a replaced
    # metered item, a period we cannot place the usage in — is UNCERTAIN, and
    # uncertainty goes to a human rather than becoming a silent write-off.
    if not subs:
        raise _NotBillableError("no_subscription_ever")
    raise _NotBillableError("coverage_unproven")


def report_meter_event_to_stripe(
    user_id: str,
    queue_id: int,
    billable_units: int,
    stripe_customer_id: str | None,
    plan: str,
    usage_at=None,
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
        # NOT terminal. This used to raise the same signal as "Stripe is off",
        # and the caller stamped reported_at on it, which permanently wrote off
        # real billable overage for a customer whose stripe_customer_id had
        # simply not been written YET. On this deployment plans are set by hand
        # and most users have no customer id at all, so that was not a corner
        # case. The caller now leaves the row unreported and the sweep holds it
        # until the user subscribes; see report_skip_trace_meter_event.
        _logger.error(
            "User %s has no stripe_customer_id: holding %d billable skip-trace "
            "unit(s) from queue %d until one exists",
            user_id[:8], billable_units, queue_id,
        )
        raise _MissingCustomerError("no_customer_id")

    # H12 (full-SaaS review): the identifier must be STABLE across webhook
    # replays so Stripe's own dedup kicks in. Keyed on (queue_id, user_id)
    # so a Stripe-side retry of the same overage always dedupes.
    stable_identifier = f"skip_trace_q{queue_id}_u{user_id}"

    # Any Stripe/network exception propagates — the durable report task's
    # autoretry_for=(Exception,) retries it, and the stable identifier above
    # makes that retry idempotent. We do NOT swallow it here.
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    # The timestamp is EXPLICIT. Omitted, Stripe stamps the event at submission
    # time, so a row that sat in the outbox for a week billed into whatever
    # period happened to be open when it was finally sent — the wrong period,
    # and for a held backlog the wrong subscription entirely. There is no
    # fallback to "now" on purpose: assert_billable has already refused anything
    # whose real time cannot be used, and silently substituting today's date is
    # the retroactive charge we are trying to stop.
    event_kwargs: dict = {
        "event_name": settings.STRIPE_METER_EVENT_NAME_SKIP_TRACE,
        "payload": {
            "value": str(billable_units),
            "stripe_customer_id": stripe_customer_id,
        },
        "identifier": stable_identifier,
    }
    if usage_at is not None:
        event_kwargs["timestamp"] = int(usage_at.timestamp())
    event = stripe.billing.MeterEvent.create(**event_kwargs)
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
    user_id, and calls report_lookups_for_user for each user.

    WHAT COUNTS AS A BILLABLE LOOKUP (owner decision, 2026-09-07):

      'completed' — reconciled normally. A hit AND a miss both bill: the
                    provider searched, and "no contact exists" is a real answer.
      'unmatched' — Tracerfy accepted the row and charged a credit for it, but
                    our address reconciliation could not map the answer back to
                    the lead. The lookup was genuinely performed and genuinely
                    paid for, so it counts against the customer's quota.

    Deliberately EXCLUDED:

      'errored'   — rejected by the dispatcher's pre-submit validation, so
                    Tracerfy never saw the row and never charged for it.
                    Billing these would charge customers for lookups that were
                    never sent. This is why ingest marks its unmatched rows
                    'unmatched' rather than reusing 'errored'.
      'queued' / 'submitting' / 'submitted' — not yet settled. The counter
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
    # Only bill 'unmatched' rows when Tracerfy demonstrably accepted EVERY row
    # we sent for this batch.
    #
    # Tracerfy both DROPS rows it cannot use and DEDUPLICATES identical
    # addresses (prod: 25 sent -> 24 uploaded). A row it never accepted also
    # never appears in the result CSV, so it lands on 'unmatched' looking exactly
    # like a row that was accepted, charged, and merely failed to reconcile.
    # Billing those would charge the customer for a lookup the provider never
    # performed -- which is NOT the decision that was made.
    #
    # There is no way to tell WHICH specific rows were dropped, so rather than
    # invent an apportionment we use the only signal that is certain: if
    # rows_uploaded covers everything we submitted, no row was dropped or
    # deduped and every unmatched row was genuinely paid for. If it does not,
    # this batch bills 'completed' rows only -- erring toward the customer.
    accepted_all = db.execute(
        text("""
            SELECT COALESCE(q.rows_uploaded, 0) >= COUNT(p.id)
            FROM skip_trace_queues q
            JOIN pending_skip_trace_rows p
              ON p.tracerfy_queue_id = q.tracerfy_queue_id
            WHERE q.tracerfy_queue_id = :qid
            GROUP BY q.rows_uploaded
        """),
        {"qid": queue_id},
    ).scalar()

    billable_states = ("completed", "unmatched") if accepted_all else ("completed",)
    if not accepted_all:
        _logger.warning(
            "Skip-trace billing queue %d: Tracerfy uploaded fewer rows than we "
            "submitted (dropped or de-duplicated), so unmatched rows are NOT "
            "billed for this batch — the customer is not charged for a lookup "
            "the provider never ran.",
            queue_id,
        )

    rows = db.execute(
        text("""
            SELECT user_id, COUNT(*) as n
            FROM pending_skip_trace_rows
            WHERE tracerfy_queue_id = :qid
              AND status = ANY(:states)
            GROUP BY user_id
            ORDER BY user_id
        """),
        {"qid": queue_id, "states": list(billable_states)},
    ).fetchall()

    # usage_at means "a time we can DEFEND billing against". NULL means we
    # cannot, and NULL is currently the honest answer for every batch.
    #
    # Three candidates were tried and all three are the wrong clock:
    #
    #   created_at    server_default=now(), i.e. this reconciliation
    #                 transaction.
    #   completed_at  set to `now` by the ingest worker a few statements before
    #                 this function runs, in the same transaction. It reads as
    #                 provider settlement and is not.
    #   submitted_at  written by the dispatcher when the batch is sent — but
    #                 _persist_submission also writes it on the RECONCILER'S
    #                 ADOPTION path, so for an adopted queue it is the adoption
    #                 time, days after the work. Not a lower bound either.
    #
    # Nothing in this system records when the provider actually performed the
    # lookups. Every one of those values can place usage from before a
    # subscription inside it, which bills a customer for something they had not
    # agreed to pay for, and can make usage past Stripe's 35-day backdating
    # limit look fresh.
    #
    # So this does not guess. The rows are kept with full detail and land in
    # needs_review, where the ops alert surfaces them and a human settles them.
    # That is the owner's stated policy for usage with no agreement behind it,
    # and it is the only answer the recorded data supports.
    #
    # TO RE-ENABLE AUTOMATIC BILLING: record a trustworthy provider-execution
    # time at dispatch (one that adoption does not overwrite) and select it
    # here. assert_billable already implements the rest of the rule and needs no
    # change — it refuses a NULL usage_at as usage_at_unknown today, and will
    # start passing the ordinary case the moment this column carries a real
    # value.
    usage_at = None

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
                    usage_at=usage_at,
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
              AND disposition = 'pending'
        """),
        {"qid": queue_id},
    ).fetchall()
    summary["outbox_ids"] = [str(r.id) for r in outbox_rows]

    return summary
