"""Account deletion beat (P3b): moves every account_deletions row to its next state.

Runs every 5 minutes as bridgeleads_system. It only READS account_deletions (114);
every state change goes through the migration-113 definer functions, which validate it.
External calls (Stripe, Resend) never run inside a database transaction: each is made,
then its outcome recorded, so a crash in between is re-driven on a later tick.

  Stripe      pending  `pending_cancel`   -> subscription set to cancel at period end
              restored `pending_uncancel` -> un-cancelled while the period is still live
              (`not_applicable` when there is no live subscription to change)
  Email       pending rows get the "scheduled for deletion" notice once (at-least-once:
              a crash after the send and before the record sends it again)
  Alerts      ops hear about a deletion still not purging 10 days after its deadline
              (the CCPA limit is 45 days from the request)

The purge itself (claim, R2, data, tombstone, completion) is P3b-2.
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


def _send_scheduled_notice(to: str, purge_after: datetime) -> None:
    """Raises on failure: the caller backs off and retries."""
    from src.workers.account_emails import _send

    when = purge_after.astimezone(UTC).strftime("%B %d, %Y")
    _send(
        to, "Your BridgeLeads account is scheduled for deletion",
        f"Your account and its data will be deleted on {when}.",
        [f"You asked us to delete your BridgeLeads account. It will be deleted, with its "
         f"leads, schedules and settings, on {when}.",
         "Your subscription will not renew, and you will not be charged again.",
         "Changed your mind? Sign in and press Restore before that date to keep everything."],
        cta=("Sign in to restore", f"{settings.FRONTEND_URL}/login"),
    )


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
    rows = _due(db, "d.status = 'pending' AND d.scheduled_email_sent_at IS NULL")
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
        "WHERE status = 'pending' AND purge_after + interval '10 days' < now()")).all()
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


def _drive_account_deletions_impl(*, stripe_api: StripeSubscriptions | None = None,
                                  send=None) -> dict:
    from src.db.session import system_sync_session

    with system_sync_session() as lock_db:
        if not lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": _LOCK_KEY}).scalar():
            _logger.info("account deletion beat: another run holds the lock")
            return {"skipped": True}
        try:
            with system_sync_session() as db:
                return {
                    "stripe": _reconcile_stripe(db, stripe_api or StripeSubscriptions()),
                    "scheduled_emails": _send_scheduled_emails(
                        db, send or _send_scheduled_notice),
                    "overdue_alerts": _alert_overdue(db),
                }
        finally:
            lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_db.commit()
