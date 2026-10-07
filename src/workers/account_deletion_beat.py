"""Account deletion beat (P3b): moves every account_deletions row to its next state.

Runs every 5 minutes as bridgeleads_system. It only READS account_deletions (114);
every state change goes through the migration-113 definer functions, which validate it.
External calls (Stripe, Resend) never run inside a database transaction: each is made,
then its outcome recorded, so a crash in between is re-driven on a later tick.

  Stripe      pending  `pending_cancel`   -> subscription set to cancel at period end
              restored `pending_uncancel` -> un-cancelled while the period is still live
              (`not_applicable` when there is no live subscription to change)
  Email       pending rows get the "scheduled for deletion" notice once billing is
              confirmed stopped (at-least-once:
              a crash after the send and before the record sends it again)
  Purge       a due deletion whose billing is stopped and whose account has no work in
              flight (until day 40) is claimed, then: R2 sweep 1 (recorded export keys +
              everything under exports/{user_id}/, listed until empty) -> data purge in
              batches -> final email to the original address -> users row tombstoned.
              The row is parked 24 h; on the reclaim: data purge again (catches an UPDATE
              in flight at the claim) -> R2 sweep 2 (fail closed) -> completed.
              Each claim stops before its lease (or the tick budget) runs out and the
              next tick resumes from the markers; a failure backs off and retries.
  Customer    after completion, the Stripe Customer is deleted once no subscription is
              live and no invoice is draft/open (never redacted, design 4.4)
  Alerts      ops hear about a deletion still not purging 10 days after its deadline
              (the CCPA limit is 45 days from the request), and one stuck in retries
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.account_deletion")

# One run at a time (pg advisory lock): overlapping ticks would double-send emails and
# race Stripe calls. Arbitrary constant, unique in this codebase.
_LOCK_KEY = 7_113_000_001
_BATCH = 50
# Subscription statuses that are already over: nothing to cancel or un-cancel.
_ENDED = frozenset({"canceled", "incomplete_expired"})


class StripeSubscriptions:
    """The two Stripe calls this task makes. Tests pass a stand-in (Stripe is an
    external, rate-limited API; precedent: `_expire_trials_impl(subscription_lookup=)`).
    Bracket access only: stripe 15's StripeObject is not a dict."""

    def status(self, subscription_id: str) -> tuple[str, bool] | None:
        """(status, cancel_at_period_end), or None when Stripe has no such subscription."""
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
        except stripe.error.InvalidRequestError as exc:
            if getattr(exc, "code", None) == "resource_missing":
                return None
            raise
        return sub["status"], bool(sub["cancel_at_period_end"])

    def set_cancel_at_period_end(self, subscription_id: str, value: bool, key: str) -> None:
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.Subscription.modify(subscription_id, cancel_at_period_end=value,
                                   idempotency_key=key)

    def customer_state(self, customer_id: str) -> str:
        """'missing' (already gone), 'open' (a live subscription, or an invoice still
        draft/open: deleting now would cancel or orphan it) or 'closable'."""
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
            if any(s["status"] not in _ENDED for s in subs.auto_paging_iter()):
                return "open"
            for status in ("draft", "open"):
                if stripe.Invoice.list(customer=customer_id, status=status, limit=1)["data"]:
                    return "open"
        except stripe.error.InvalidRequestError as exc:
            if getattr(exc, "code", None) == "resource_missing":
                return "missing"
            raise
        return "closable"

    def delete_customer(self, customer_id: str, key: str) -> None:
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe.Customer.delete(customer_id, idempotency_key=key)
        except stripe.error.InvalidRequestError as exc:
            if getattr(exc, "code", None) != "resource_missing":
                raise


class R2Store:
    """The account's export files in R2. Tests pass an in-memory stand-in."""

    def list(self, prefix: str) -> list[str]:
        from src.utils.data_exporter import DataExporter
        return DataExporter().list_r2_keys(prefix)

    def delete(self, key: str) -> bool:
        from src.utils.data_exporter import DataExporter
        return DataExporter().delete_from_r2(key)


def _scheduled_notice(purge_after: datetime) -> tuple:
    """(subject, preheader, lines, cta) of the "scheduled for deletion" notice."""
    when = purge_after.astimezone(UTC).strftime("%B %d, %Y")
    return (
        "Your BridgeLeads account is scheduled for deletion",
        f"Your account and its data will be deleted on {when}.",
        [f"You asked us to delete your BridgeLeads account. It will be deleted, with its "
         f"leads, schedules and settings, on {when}.",
         "Your subscription will not renew, and you will not be charged again.",
         "Changed your mind? Sign in and press Restore before that date to keep everything."],
        ("Sign in to restore", f"{settings.FRONTEND_URL}/login"),
    )


def _send_scheduled_notice(to: str, purge_after: datetime) -> None:
    """Raises on failure: the caller backs off and retries."""
    from src.workers.account_emails import _send

    subject, preheader, lines, cta = _scheduled_notice(purge_after)
    _send(to, subject, preheader, lines, cta=cta)


def _record(db, deletion_id, phase: str, frm=None, to=None, error=None) -> bool:
    ok = db.execute(
        text("SELECT record_deletion_progress(:d, NULL, :p, :f, :t, :e)"),
        {"d": deletion_id, "p": phase, "f": frm, "t": to, "e": error},
    ).scalar()
    db.commit()
    return bool(ok)


def _due(db, where: str) -> list:
    return db.execute(text(
        "SELECT d.id, d.user_id, d.status, d.stripe_state, d.purge_after, "  # noqa: S608 - callers pass fixed literals
        "       u.stripe_subscription_id "
        "  FROM account_deletions d JOIN users u ON u.id = d.user_id "
        f" WHERE ({where}) AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now()) "
        " ORDER BY d.requested_at LIMIT :n"), {"n": _BATCH}).all()


def _reconcile_stripe(db, stripe_api: StripeSubscriptions) -> int:
    done = 0
    rows = _due(db, "(d.status = 'pending' AND d.stripe_state = 'pending_cancel') OR "
                    "(d.status = 'restored' AND d.stripe_state = 'pending_uncancel')")
    db.rollback()  # no transaction stays open across the Stripe calls
    for row in rows:
        cancel = row.status == "pending"
        frm = "pending_cancel" if cancel else "pending_uncancel"
        try:
            to = "not_applicable"
            sub = (stripe_api.status(row.stripe_subscription_id)
                   if row.stripe_subscription_id else None)
            if sub is not None and sub[0] not in _ENDED:
                if sub[1] != cancel:
                    stripe_api.set_cancel_at_period_end(
                        row.stripe_subscription_id, cancel,
                        f"acctdel-{row.id}-{'cancel' if cancel else 'uncancel'}")
                to = "cancel_set" if cancel else "uncancel_set"
        except Exception as exc:  # noqa: BLE001 - recorded and retried with backoff
            _logger.warning("account deletion %s: Stripe %s failed: %s",
                            row.id, frm, type(exc).__name__)
            _record(db, row.id, "error", error=f"stripe {frm}: {type(exc).__name__}")
            continue
        # False = the row moved on meanwhile (e.g. restored): a later tick re-reads it.
        if _record(db, row.id, "stripe", frm, to):
            done += 1
    return done


def _send_scheduled_emails(db, send) -> int:
    if not settings.RESEND_API_KEY:
        # A config error, not a delivery failure: leave the notices pending.
        _logger.error("RESEND_API_KEY unset: account-deletion notices left pending")
        return 0
    from src.db.models import User
    from src.workers.delivery import _email_error_summary

    sent = 0
    # Only once billing is confirmed stopped: the notice says the plan will not renew.
    rows = _due(db, "d.status = 'pending' AND d.scheduled_email_sent_at IS NULL "
                    "AND d.stripe_state IN ('cancel_set', 'not_applicable')")
    for row in rows:
        email = db.get(User, row.user_id).email  # ORM: decrypted
        db.rollback()
        try:
            send(email, row.purge_after)
        except Exception as exc:  # noqa: BLE001 - recorded and retried with backoff
            _logger.warning("account deletion %s: scheduled notice failed: %s",
                            row.id, _email_error_summary(exc))
            _record(db, row.id, "error", error="scheduled notice: "
                    + _email_error_summary(exc))
            continue
        if _record(db, row.id, "scheduled_email_sent"):
            sent += 1
    return sent


def _alert_overdue(db) -> int:
    from src.workers.ops_alerts import send_ops_alert

    rows = db.execute(text(
        "SELECT id, status, stripe_state, attempts FROM account_deletions "
        "WHERE status = 'pending' AND purge_after + interval '10 days' < now() "
        # send_ops_alert's 6 h cooldown per deletion keeps this from repeating each tick.
        "ORDER BY purge_after LIMIT :n"), {"n": _BATCH}).all()
    db.rollback()
    for row in rows:
        send_ops_alert(
            "account_deletion_overdue", str(row.id),
            "Account deletion overdue",
            f"Deletion {row.id} is still {row.status} more than 10 days past its deadline "
            f"(stripe_state={row.stripe_state}, attempts={row.attempts}). The CCPA limit is "
            "45 days from the request: check Stripe and the purge task.",
        )
    return len(rows)


# ── The purge (P3b-2) ────────────────────────────────────────────────────────────

_LEASE_SECONDS = 900
_LEASE_MARGIN = 120        # never start a phase this close to the end of the lease
_TICK_BUDGET = 180         # seconds of purge work per tick (task soft limit is 240)
_PURGE_BATCH = 5000
_MAX_CLAIMS = 5
_STUCK_ATTEMPTS = 6
# Work that would still write the account's rows: the purge waits for it (until day 40,
# then proceeds under the write fence; the CCPA limit is 45 days).
_IN_FLIGHT = """
    EXISTS (SELECT 1 FROM jobs j WHERE j.user_id = d.user_id
               AND j.status NOT IN ('done', 'failed', 'cancelled'))
 OR EXISTS (SELECT 1 FROM batch_runs b WHERE b.user_id = d.user_id
               AND b.status IN ('pending', 'running'))
 OR EXISTS (SELECT 1 FROM pending_skip_trace_rows p WHERE p.user_id = d.user_id
               AND p.status IN ('submitting', 'submitted'))
 OR EXISTS (SELECT 1 FROM skip_trace_queues q WHERE q.user_id = d.user_id
               AND q.status = 'pending')
"""


class _OutOfTimeError(Exception):
    """The lease or the tick budget is nearly spent: stop; the next tick resumes."""


def _defer_in_flight(db) -> int:
    ids = db.execute(text(
        "SELECT d.id FROM account_deletions d "  # noqa: S608 - fixed literals
        " WHERE d.status = 'pending' AND d.purge_after <= now() "
        "   AND d.purge_after + interval '10 days' > now() "
        "   AND d.stripe_state IN ('cancel_set', 'not_applicable') "
        "   AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now()) "
        f"  AND ({_IN_FLIGHT}) ORDER BY d.purge_after LIMIT :n"), {"n": _BATCH}).scalars().all()
    db.rollback()
    for did in ids:
        _record(db, did, "error", error="waiting for in-flight work")
    return len(ids)


def _sweep_r2(r2: R2Store, uid: str, keys: list[str], check_time) -> None:
    """Delete the given keys and everything under exports/{uid}/, then require the
    prefix to list empty. Raises (= not swept) on any failure: fail closed. Checks the
    time on every page, so a huge prefix pauses and resumes instead of outliving
    the lease (deletes are idempotent: a resumed sweep just lists again)."""
    for key in keys:
        check_time()
        if not r2.delete(key):
            raise RuntimeError("R2 delete failed")
    prefix = f"exports/{uid}/"
    while True:  # bounded by check_time: a huge prefix pauses and resumes
        check_time()
        page = r2.list(prefix)
        if not page:
            return
        for key in page:
            if not r2.delete(key):
                raise RuntimeError("R2 delete failed")


def _final_notice() -> tuple:
    return (
        "Your BridgeLeads account has been deleted",
        "Your account and its data have been deleted.",
        ["Your BridgeLeads account has been deleted, as you asked. Its leads, schedules, "
         "settings and stored exports are gone, and you will not be charged again.",
         "We keep billing records for 7 years, as the law requires. Files you downloaded "
         "earlier are not affected.",
         "This address can be used to create a new account at any time."],
        None,
    )


def _send_final_notice(to: str) -> None:
    from src.workers.account_emails import _send

    subject, preheader, lines, cta = _final_notice()
    _send(to, subject, preheader, lines, cta=cta)


def _tombstone(db, uid: str, deletion_id: str, token) -> None:
    """The users row keeps only what billing needs (matrix §2); the placeholder email
    recomputes email_hmac (ORM validator), freeing the address. Committed only while
    this run still holds the live claim (users is not write-fenced; one run at a
    time under the advisory lock, and the task's hard limit is far below the lease,
    so this is a belt)."""
    import secrets

    from src.api.auth import hash_password
    from src.db.models import User

    user = db.get(User, uid)
    user.email = f"deleted+{uid}@invalid"
    for col in ("name", "first_name", "last_name", "timezone", "api_key_hash",
                "mfa_secret_encrypted", "mfa_enrolled_at", "mfa_last_totp_counter",
                "referral_code"):
        setattr(user, col, None)
    user.mfa_enabled = False
    user.is_admin = False
    user.notification_prefs = {}
    user.password_hash = hash_password(secrets.token_urlsafe(32))
    user.revoked_at = datetime.now(UTC)
    db.flush()
    if not db.execute(text(
            "SELECT 1 FROM account_deletions WHERE id = :d AND status = 'purging' "
            "AND claim_token = :t AND claimed_until > clock_timestamp()"),
            {"d": deletion_id, "t": token}).first():
        db.rollback()
        raise _OutOfTimeError
    db.commit()


def _call(db, sql: str, params: dict):
    value = db.execute(text(sql), params).scalar()
    db.commit()
    return value


def _advance(db, claim, r2: R2Store, send_final, deadline: float) -> str:
    """Drive one claimed deletion as far as its markers allow. Returns the phase reached."""
    import time

    from src.scrapers.enrichment.skip_trace import pending_row_subject_key

    did, uid, token = str(claim.deletion_id), str(claim.user_id), claim.claim_token
    p = {"d": did, "t": token}

    def check_time() -> None:
        if time.monotonic() >= deadline:  # >=: Windows' clock ticks ~15 ms
            raise _OutOfTimeError

    row = db.execute(text("SELECT * FROM account_deletions WHERE id = :d"), p).one()
    if row.r2_first_sweep_at is None:
        keys = db.execute(text(
            "SELECT export_key FROM jobs WHERE user_id = :u AND export_key IS NOT NULL "
            "UNION SELECT combined_export_key FROM batch_runs "
            " WHERE user_id = :u AND combined_export_key IS NOT NULL"), {"u": uid}).scalars().all()
        db.rollback()
        _sweep_r2(r2, uid, list(keys), check_time)
        check_time()
        _call(db, "SELECT record_deletion_progress(:d, :t, 'r2_first_sweep')", p)

    # The user's cached vendor answers, keyed from the pending rows BEFORE their scrub
    # (on a re-run the scrubbed rows give keys that match nothing: harmless).
    pending = db.execute(text(
        "SELECT user_id, property_address, city, state, trace_type, first_name, last_name "
        "  FROM pending_skip_trace_rows WHERE user_id = :u"), {"u": uid}).all()
    cache_keys = sorted({pending_row_subject_key(r) for r in pending})
    db.rollback()
    check_time()
    while not _call(db, "SELECT purge_account_data(:d, :t, :k, :b)",
                    {**p, "k": cache_keys, "b": _PURGE_BATCH}):
        check_time()

    if row.tombstoned_at is None:
        check_time()
        if row.final_email_sent_at is None:
            from src.db.models import User

            email = db.get(User, uid).email  # the original address, before the tombstone
            db.rollback()
            send_final(email)
            _call(db, "SELECT record_deletion_progress(:d, :t, 'final_email_sent')", p)
        _tombstone(db, uid, did, token)
        # Releases the lease and parks the row until 24 h after the first sweep.
        _call(db, "SELECT record_deletion_progress(:d, :t, 'tombstoned')", p)
        return "tombstoned"

    # Second pass, >= 24 h after the first sweep: the data purge above ran again.
    check_time()
    _sweep_r2(r2, uid, [], check_time)
    _call(db, "SELECT record_deletion_progress(:d, :t, 'r2_final_sweep')", p)
    _call(db, "SELECT complete_account_deletion(:d, :t)", p)
    return "completed"


def _run_purges(db, r2: R2Store, send_final) -> dict:
    import time

    started = time.monotonic()
    phases: dict[str, int] = {}
    for _ in range(_MAX_CLAIMS):
        if time.monotonic() - started > _TICK_BUDGET:
            break
        claim = db.execute(text(
            "SELECT * FROM claim_account_deletion(make_interval(secs => :s))"),
            {"s": _LEASE_SECONDS}).one_or_none()
        db.commit()
        if claim is None:
            break
        deadline = min(time.monotonic() + _LEASE_SECONDS - _LEASE_MARGIN,
                       started + _TICK_BUDGET)
        try:
            phase = _advance(db, claim, r2, send_final, deadline)
        except _OutOfTimeError:
            db.rollback()
            phase = "paused"
            # Hand the lease back (short backoff) so the next tick resumes from the
            # markers instead of waiting out the lease. A lost claim fails here: fine.
            try:
                _call(db, "SELECT record_deletion_progress(:d, :t, 'error', NULL, NULL, :e)",
                      {"d": str(claim.deletion_id), "t": claim.claim_token,
                       "e": "paused: out of time"})
            except Exception:  # noqa: BLE001 - the lease lapses on its own
                db.rollback()
        except Exception as exc:  # noqa: BLE001 - recorded, retried with backoff
            db.rollback()
            phase = "error"
            reason = type(exc).__name__ + (f" {exc.orig.pgcode}" if hasattr(exc, "orig")
                                           and getattr(exc.orig, "pgcode", None) else "")
            _logger.warning("account deletion %s: purge step failed: %s",
                            claim.deletion_id, reason)
            try:
                _call(db, "SELECT record_deletion_progress(:d, :t, 'error', NULL, NULL, :e)",
                      {"d": str(claim.deletion_id), "t": claim.claim_token,
                       "e": f"purge: {reason}"})
            except Exception:  # noqa: BLE001 - the lease lapses on its own
                db.rollback()
                _logger.exception("account deletion %s: could not record the error",
                                  claim.deletion_id)
        phases[phase] = phases.get(phase, 0) + 1
    return phases


def _close_stripe_customers(db, stripe_api: StripeSubscriptions) -> int:
    """After completion: delete the Stripe Customer once nothing is live or open.
    Finalized invoices keep their own copy of the customer details (the tax record)."""
    rows = db.execute(text(
        "SELECT d.id, d.stripe_state, u.stripe_customer_id "
        "  FROM account_deletions d JOIN users u ON u.id = d.user_id "
        " WHERE d.status = 'completed' AND d.stripe_state IN ('cancel_set', 'not_applicable') "
        "   AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now()) "
        " ORDER BY d.completed_at LIMIT :n"), {"n": _BATCH}).all()
    db.rollback()
    done = 0
    for row in rows:
        try:
            if row.stripe_customer_id:
                state = stripe_api.customer_state(row.stripe_customer_id)
                if state == "open":
                    _record(db, row.id, "error",
                            error="waiting for the subscription to end / invoices to close")
                    continue
                if state == "closable":
                    stripe_api.delete_customer(row.stripe_customer_id,
                                               f"acctdel-{row.id}-customer")
        except Exception as exc:  # noqa: BLE001 - recorded, retried with backoff
            _record(db, row.id, "error", error=f"stripe customer: {type(exc).__name__}")
            continue
        if _record(db, row.id, "stripe", row.stripe_state, "customer_deleted"):
            done += 1
    return done


def _alert_stuck(db) -> int:
    from src.workers.ops_alerts import send_ops_alert

    rows = db.execute(text(
        "SELECT id, attempts, last_error FROM account_deletions "
        "WHERE status = 'purging' AND attempts >= :stuck "
        "ORDER BY attempts DESC LIMIT :n"), {"stuck": _STUCK_ATTEMPTS, "n": _BATCH}).all()
    db.rollback()
    for row in rows:
        send_ops_alert(
            "account_deletion_stuck", str(row.id), "Account deletion stuck",
            f"Deletion {row.id} has been retried {row.attempts} times; last error: "
            f"{(row.last_error or '')[:200]}",
        )
    return len(rows)


def _drive_account_deletions_impl(*, stripe_api: StripeSubscriptions | None = None,
                                  send=None, r2: R2Store | None = None,
                                  send_final=None) -> dict:
    from src.db.session import system_sync_session

    with system_sync_session() as lock_db:
        if not lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": _LOCK_KEY}).scalar():
            _logger.info("account deletion beat: another run holds the lock")
            return {"skipped": True}
        try:
            stripe_api = stripe_api or StripeSubscriptions()
            with system_sync_session() as db:
                return {
                    "stripe": _reconcile_stripe(db, stripe_api),
                    "scheduled_emails": _send_scheduled_emails(
                        db, send or _send_scheduled_notice),
                    "deferred": _defer_in_flight(db),
                    "purges": _run_purges(db, r2 or R2Store(), send_final or _send_final_notice),
                    "customers_deleted": _close_stripe_customers(db, stripe_api),
                    "overdue_alerts": _alert_overdue(db),
                    "stuck_alerts": _alert_stuck(db),
                }
        finally:
            lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_db.commit()
