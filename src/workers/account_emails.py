"""Account email-change emails, and the outbox that finishes a confirmed change.

Request time (best-effort FastAPI background tasks, like the password reset; the
user can simply ask again):
  send_email_change_verification      the one-hour link, to the NEW address
  send_email_change_requested_notice  heads-up to the CURRENT address

After confirmation the pending_email_changes row is the outbox (beat, every 60s,
like pending_registrations): the old-address "your email was changed" notice and
the Stripe customer email sync are retried with backoff until they succeed, and
ops are alerted if one finally fails. A confirmed change can therefore never
silently skip telling the previous owner of the account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import resend

from src.config import settings
from src.utils.email_layout import build_payload, paragraph, render_email, text_footer
from src.utils.logger import setup_logger

_logger = setup_logger("worker.account_emails")

_OUTBOX_BATCH = 50
_OUTBOX_MAX_ATTEMPTS = 8
_OUTBOX_BACKOFF_BASE = 60     # seconds
_OUTBOX_BACKOFF_CAP = 3600

_NOT_YOU = (
    "If this was not you, reset your password right away and contact "
    f"{settings.SUPPORT_EMAIL}."
)


def _send(to: str, subject: str, preheader: str, lines: list[str],
          cta: tuple[str, str] | None = None, cta_note: str | None = None) -> None:
    html_body = render_email(
        title=subject, preheader=preheader, heading=subject,
        blocks=[paragraph(line) for line in lines], cta=cta, cta_note=cta_note,
        footer_note=_NOT_YOU,
    )
    text = "\n\n".join(lines + ([f"{cta[0]}: {cta[1]}"] if cta else []))
    resend.Emails.send(build_payload(
        to=[to], subject=subject, html_body=html_body,
        text_body=text + "\n\n" + text_footer(footer_note=_NOT_YOU),
    ))


def send_email_change_verification(new_email: str, link: str) -> None:
    """Soft-fails (logged): the response must not depend on delivery."""
    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not configured, skipping email-change verification")
        return
    try:
        _send(
            new_email, "Confirm your new email address",
            "Confirm this address for your BridgeLeads account. The link expires in 1 hour.",
            ["Someone asked to use this address for a BridgeLeads account. Confirm it "
             "below to finish the change."],
            cta=("Confirm Email", link),
            cta_note="This link expires in 1 hour and can be used once. Confirming "
                     "signs you out of all devices; sign in again with this address.",
        )
    except Exception as exc:  # noqa: BLE001 — logged, never fails the request
        from src.workers.delivery import _email_error_summary
        _logger.error("email-change verification send failed: %s", _email_error_summary(exc))


def send_email_change_requested_notice(current_email: str) -> None:
    """Soft-fails (logged). Tells the current address a change was requested."""
    if not settings.RESEND_API_KEY:
        return
    try:
        _send(
            current_email, "Email change requested",
            "Someone asked to change the email on your BridgeLeads account.",
            ["Someone signed in to your BridgeLeads account asked to change its email "
             "address. Nothing changes until the new address is confirmed."],
        )
    except Exception as exc:  # noqa: BLE001
        from src.workers.delivery import _email_error_summary
        _logger.error("email-change requested notice failed: %s", _email_error_summary(exc))


def _send_email_changed_notice(old_email: str) -> None:
    """Raises on failure: the outbox classifies and retries."""
    _send(
        old_email, "Your email address was changed",
        "The email on your BridgeLeads account was changed.",
        ["The email address on your BridgeLeads account was changed and this address "
         "no longer signs in. All devices were signed out."],
    )


def _sync_stripe_email(customer_id: str, new_email: str) -> None:
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.Customer.modify(customer_id, email=new_email)


def _drain_email_change_outbox_impl() -> None:
    """Finish confirmed email changes: old-address notice + Stripe email sync.

    One short transaction per row, FOR UPDATE SKIP LOCKED so concurrent beats
    never double-send. Each side effect has its own state, so a Stripe outage
    never re-sends a notice that already went out.
    """
    from sqlalchemy import or_, select

    from src.db.models import PendingEmailChange, User
    from src.db.session import system_sync_session
    from src.workers.delivery import _email_error_summary, _is_retryable_email_error

    if not settings.RESEND_API_KEY:
        # A config error, not a delivery failure: leave rows pending (they finish
        # once the key is set) and burn no attempts, like the signup outbox.
        _logger.error("RESEND_API_KEY unset: email-change outbox left pending")
        return
    now = datetime.now(UTC)
    with system_sync_session() as db:
        ids = list(db.execute(
            select(PendingEmailChange.id).where(
                PendingEmailChange.status == "confirmed",
                or_(PendingEmailChange.notice_state == "pending",
                    PendingEmailChange.stripe_state == "pending"),
                PendingEmailChange.next_outbox_attempt_at <= now,
            ).order_by(PendingEmailChange.confirmed_at).limit(_OUTBOX_BATCH)
        ).scalars())

    for row_id in ids:
        with system_sync_session() as db:
            try:
                row = db.execute(
                    select(PendingEmailChange).where(PendingEmailChange.id == row_id)
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if row is None:
                    continue
                failed: list[str] = []
                retryable = True
                if row.notice_state == "pending":
                    try:
                        _send_email_changed_notice(row.old_email)
                        row.notice_state = "sent"
                    except Exception as exc:  # noqa: BLE001 — classified below
                        failed.append(f"notice: {_email_error_summary(exc)}")
                        retryable = retryable and _is_retryable_email_error(exc)
                if row.stripe_state == "pending":
                    customer_id = db.execute(
                        select(User.stripe_customer_id).where(User.id == row.user_id)
                    ).scalar_one_or_none()
                    # A later confirmed change owns Stripe's email; syncing this
                    # older one after it would write back an obsolete address.
                    superseded = db.execute(
                        select(PendingEmailChange.id).where(
                            PendingEmailChange.user_id == row.user_id,
                            PendingEmailChange.status == "confirmed",
                            PendingEmailChange.confirmed_at > row.confirmed_at,
                        ).limit(1)
                    ).first() is not None
                    if not customer_id or superseded:
                        row.stripe_state = "skipped"
                    else:
                        try:
                            _sync_stripe_email(customer_id, row.new_email)
                            row.stripe_state = "synced"
                        except Exception as exc:  # noqa: BLE001 — retried below
                            failed.append(f"stripe: {type(exc).__name__}")
                if failed:
                    row.outbox_attempts += 1
                    if retryable and row.outbox_attempts < _OUTBOX_MAX_ATTEMPTS:
                        backoff = min(_OUTBOX_BACKOFF_CAP,
                                      _OUTBOX_BACKOFF_BASE * 2 ** (row.outbox_attempts - 1))
                        row.next_outbox_attempt_at = now + timedelta(seconds=backoff)
                        _logger.warning("email-change outbox retry %d for %s: %s",
                                        row.outbox_attempts, row_id, "; ".join(failed))
                    else:
                        if row.notice_state == "pending":
                            row.notice_state = "failed"
                        if row.stripe_state == "pending":
                            row.stripe_state = "failed"
                        _logger.error("email-change outbox FAILED for %s: %s",
                                      row_id, "; ".join(failed))
                        try:
                            from src.workers.ops_alerts import send_ops_alert
                            send_ops_alert(
                                "email_change_outbox", str(row_id),
                                "Email change follow-up failed",
                                "A confirmed email change could not finish: "
                                + "; ".join(failed) + ". Notify the old address and/or "
                                "update the Stripe customer email by hand.",
                            )
                        except Exception:  # noqa: BLE001 — alert is best-effort
                            pass
                db.commit()
            except Exception:  # noqa: BLE001 — isolate one bad row, keep draining
                db.rollback()
                _logger.exception("email-change outbox error for %s", row_id)
