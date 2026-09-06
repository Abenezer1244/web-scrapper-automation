"""Email delivery: send job results via Resend after successful export."""

import requests
import resend
from celery.exceptions import SoftTimeLimitExceeded

from src.config import settings
from src.utils.email_layout import (
    build_payload,
    callout,
    header_text,
    paragraph,
    render_email,
    stat,
    text_footer,
)
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.delivery")

resend.api_key = settings.RESEND_API_KEY

# Resend SDK errors that are worth retrying (transient) vs permanent. The lead
# delivery email is a PURCHASED channel, not a courtesy ping, so a transient
# Resend/network blip must not silently drop it (Codex). Permanent failures
# (bad/missing key, validation) are NOT retried — retrying can't fix them.
_PERMANENT_RESEND_ERRORS = (
    "InvalidApiKeyError",
    "MissingApiKeyError",
    "MissingRequiredFieldsError",
    "ValidationError",
)

# Retry backoff base (seconds): waits ~5s, 25s, 125s between attempts.
_BACKOFF_BASE = 5


def _is_retryable_email_error(exc: Exception) -> bool:
    """True if a Resend send failure is transient and worth a Celery retry.

    Retry: network/transport errors (requests.RequestException), a hung send cut
    off by the task's soft time limit (the Resend SDK POSTs with no timeout — a
    hang IS the transient case the limit exists for), and server-side Resend
    errors whose HTTP status is 408/409/429 or 5xx. Do NOT retry permanent
    client errors (auth, validation, malformed payload) — they fail identically
    on every attempt and just waste the retry budget.
    """
    if isinstance(exc, (requests.RequestException, SoftTimeLimitExceeded)):
        return True
    # Celery's soft time limit firing mid-send is the DEFINITION of transient:
    # soft_time_limit exists on this task only because the Resend SDK issues its
    # POST with no timeout, so a hung request is exactly what it catches. Without
    # this branch that exception fell through to the permanent default below and
    # a hung send was logged "GAVE UP" with no retry, silently dropping a
    # purchased delivery email (found by Codex).
    if isinstance(exc, SoftTimeLimitExceeded):
        return True
    if type(exc).__name__ in _PERMANENT_RESEND_ERRORS:
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if isinstance(code, int):
        return code in (408, 409, 429) or code >= 500
    # Generic server-side ResendError without a numeric code → treat the
    # explicitly server-side ApplicationError as transient; everything else
    # (incl. client-side ValueError for a missing arg) as permanent.
    return type(exc).__name__ == "ApplicationError"


def _email_error_summary(exc: Exception) -> str:
    """Type + status only — never the raw Resend message, which can echo the
    recipient/from/domain back (PII in logs, Codex)."""
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}" + (f" (code={code})" if code is not None else "")


def _build_lead_delivery_email(
    scraper_name: str, record_count: int, download_url: str, fmt: str,
    summary_message: str | None = None, link_expires: bool = True,
) -> tuple[str, str, str]:
    """Build (subject, html_body, text_body) for the lead-delivery email.

    Pure (no I/O) so it's unit-testable and the retryable send task can rebuild
    the identical message on every attempt.
    """
    # scraper_name is user input (ScraperConfig.name) and lands in a HEADER here,
    # not in HTML, so it needs control-character stripping rather than escaping.
    safe_subject_name = header_text(scraper_name, limit=80)
    subject = f"Your {safe_subject_name} results are ready: {record_count:,} records"

    # DNC/TCPA disclaimer is shown HERE (and the download UI), not inside the CSV,
    # because a disclaimer row corrupts a dialer/spreadsheet import. Single source
    # in constants so every surface shows identical copy.
    from src.config.constants import DNC_DISCLAIMER

    fmt_label = fmt.upper()
    footer_note = (
        f"You are receiving this because you set up automated delivery for "
        f"{scraper_name}. Manage delivery settings in your BridgeLeads account."
    )
    # Batch deliveries link to the in-app batch page (no expiry); per-job links
    # are 48h presigns. Wrong copy on a batch email erodes trust (Codex P2), so
    # the expiry note is omitted entirely rather than shown unconditionally.
    expiry_note = "This download link expires in 48 hours." if link_expires else None
    expiry_text = f"{expiry_note}\n\n" if expiry_note else ""
    summary_text = f"{summary_message}\n\n" if summary_message else ""

    blocks = [paragraph(scraper_name, muted=True)]
    if summary_message:
        blocks.append(paragraph(summary_message))
    blocks += [
        stat(f"{record_count:,}", "Records found"),
        callout(DNC_DISCLAIMER, tone="warning"),
    ]

    html_body = render_email(
        title=subject,
        preheader=f"{record_count:,} records ready to download.",
        heading="Your results are ready",
        blocks=blocks,
        cta=(f"Download {fmt_label}", download_url),
        cta_note=expiry_note,
        footer_note=footer_note,
    )

    text_body = (
        f"Your {scraper_name} results are ready.\n\n"
        f"{record_count:,} records found.\n\n"
        f"{summary_text}"
        f"Download ({fmt_label}): {download_url}\n\n"
        f"{expiry_text}"
        f"{DNC_DISCLAIMER}\n\n"
        f"{text_footer(footer_note=footer_note)}"
    )

    return subject, html_body, text_body


@app.task(
    name="src.workers.delivery.deliver_job_email",
    bind=True,
    max_retries=3,
    # Backoff is applied manually via the explicit countdown on self.retry below
    # (status-aware), so the autoretry_for/retry_backoff machinery is intentionally
    # NOT used here.
    # The Resend SDK issues a requests call with NO timeout (Codex), so without a
    # task time limit a hung POST could pin an email worker indefinitely. These
    # caps bound a single attempt; Celery retries handle the transient case.
    soft_time_limit=30,
    time_limit=45,
)
def deliver_job_email(
    self,
    job_id: str,
    scraper_name: str,
    record_count: int,
    download_url: str,
    recipient_emails: list[str],
    fmt: str = "csv",
    summary_message: str | None = None,
    link_expires: bool = True,
) -> None:
    """Send the lead-delivery email via Resend, with retries.

    Enqueued (``.delay()``) by run_scrape_job and the batch finalizer AFTER the
    export is durably in R2. Email is a purchased delivery channel, so transient
    Resend/network failures are retried (status-aware) instead of being swallowed
    on the first blip like the old inline best-effort send. Enqueue is at-most-once
    (the per-job done-CAS and per-batch delivery_started_at CAS each enqueue this
    once), and Celery retries only on a raised exception. Delivery itself is
    at-least-once like the webhook task: with acks_late a worker crash after a
    successful Resend send but before ack could redeliver and re-send. That's an
    accepted bar for an email channel (the export is the source of truth); a true
    exactly-once would need provider idempotency or a durable outbox.

    download_url is built by the caller (tokenized 48h link for a job; the in-app
    batch page for a batch) — its TTL vastly exceeds the retry window.
    """
    if not recipient_emails:
        _logger.info("No delivery emails for job %s — skipping", job_id)
        return

    if not settings.RESEND_API_KEY:
        _logger.warning(
            "RESEND_API_KEY not configured — skipping email delivery for job %s", job_id
        )
        return

    subject, html_body, text_body = _build_lead_delivery_email(
        scraper_name, record_count, download_url, fmt, summary_message=summary_message, link_expires=link_expires
    )

    try:
        resend.Emails.send(build_payload(
            to=recipient_emails,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        ))
    except Exception as exc:
        attempt = self.request.retries + 1
        summary = _email_error_summary(exc)
        if _is_retryable_email_error(exc) and attempt <= self.max_retries:
            _logger.warning(
                "Delivery email for job %s failed (attempt %d/%d, retryable): %s",
                job_id, attempt, self.max_retries + 1, summary,
            )
            raise self.retry(exc=exc, countdown=_BACKOFF_BASE * (5 ** self.request.retries))
        # Permanent failure, or retries exhausted — give up WITHOUT raising so a
        # delivery failure never marks the (already-done) scrape job as errored.
        # Surface it to ops so a silently-undelivered purchased email is visible
        # (the export IS in storage; the user can still download in-app).
        _logger.error(
            "Delivery email for job %s GAVE UP after %d attempt(s): %s",
            job_id, attempt, summary,
        )
        from src.workers.ops_alerts import send_ops_alert
        send_ops_alert(
            "email_delivery", job_id,
            "Lead delivery email failed",
            f"Delivery email for job {job_id} ({scraper_name}) gave up after "
            f"{attempt} attempt(s): {summary}. The export is in storage; the "
            f"user can still download it in-app.",
        )
        return

    _logger.info(
        "Delivery email sent for job %s to %d recipients (%d records)",
        job_id, len(recipient_emails), record_count,
    )


def _send_payment_failed_email(email: str, attempt_count: int) -> None:
    """Notify a user that their payment failed.

    Called by the Stripe webhook handler on invoice.payment_failed events.
    Soft-fails — never raises so the webhook always returns 200 to Stripe.
    """
    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not configured — skipping payment failed email to %s", email)
        return

    ordinal = {1: "first", 2: "second", 3: "third"}.get(attempt_count, f"{attempt_count}th")
    subject = "Action required: your BridgeLeads payment failed"
    url = f"{settings.FRONTEND_URL}/settings?tab=billing"
    alert = (
        f"This is the {ordinal} failed payment attempt. Update your payment "
        f"method to keep your subscription active."
    )

    html_body = render_email(
        title=subject,
        preheader="Update your payment method to keep your subscription active.",
        heading="Payment failed",
        blocks=[
            callout(alert, tone="warning"),
            paragraph(
                "We were not able to charge your card. Once the payment method "
                "is updated we will retry automatically."
            ),
        ],
        cta=("Update Payment Method", url),
    )

    text_body = (
        f"Your BridgeLeads payment failed (attempt {attempt_count}).\n\n"
        f"{alert}\n\n"
        f"Update your payment method: {url}\n\n"
        f"{text_footer()}"
    )

    try:
        resend.Emails.send(build_payload(
            to=[email], subject=subject, html_body=html_body, text_body=text_body,
        ))
        _logger.info("Payment failed email sent to %s (attempt %d)", email, attempt_count)
    except Exception as exc:
        _logger.error("Failed to send payment failed email to %s: %s", email, exc)


def send_lockout_notification(email: str, failure_count: int, ip: str) -> None:
    """Notify a user that their account is under a brute-force attack.

    Soft-fails — never raises so auth flow is not disrupted.
    """
    if not settings.RESEND_API_KEY:
        return

    # email is the recipient, never rendered in the body. ip IS rendered, and the
    # layout's callout() escapes it (belt) as it is attacker-influenced.
    subject = "Security alert: suspicious login activity on your BridgeLeads account"
    alert = (
        f"We detected {failure_count} failed login attempts on your account "
        f"from IP {ip}. Your account has been temporarily locked for your "
        f"protection."
    )

    html_body = render_email(
        title=subject,
        preheader="Your account was temporarily locked after repeated failed logins.",
        heading="Suspicious login activity",
        blocks=[
            callout(alert, tone="warning"),
            paragraph(
                "If this was you, wait a few minutes and try again. If you did "
                "not attempt to log in, change your password as soon as you can."
            ),
        ],
        footer_note="This is an automated security notification from BridgeLeads.",
    )

    text_body = (
        f"Security alert: {failure_count} failed login attempts detected on your "
        f"BridgeLeads account from IP {ip}.\n\n"
        "Your account has been temporarily locked.\n\n"
        "If this was you, wait a few minutes and try again. If you did not "
        "attempt to log in, change your password as soon as you can.\n\n"
        + text_footer(
            footer_note="This is an automated security notification from BridgeLeads."
        )
    )

    try:
        resend.Emails.send(build_payload(
            to=[email], subject=subject, html_body=html_body, text_body=text_body,
        ))
        _logger.info("Lockout notification sent to %s (%d failures from %s)", email, failure_count, ip)
    except Exception as exc:
        _logger.error("Failed to send lockout notification to %s: %s", email, exc)


def send_password_reset_email(email: str, reset_link: str) -> None:
    """A3: send a password-reset link via Resend.

    Called best-effort by POST /auth/forgot-password. Soft-fails — never
    raises so the caller's enumeration-safe 200 is unaffected and a send
    failure cannot leak whether the account exists. The link carries a
    short-lived single-use reset token (~30 min) and resetting signs out
    all of the account's existing sessions.
    """
    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not configured — skipping password reset email to %s", email)
        return

    # reset_link is a server-built FRONTEND_URL + signed token. render_email
    # escapes the href anyway so it can never be an HTML-injection vector.
    subject = "Reset your BridgeLeads password"
    footer_note = (
        "If you did not request this, you can safely ignore this email. Your "
        "password will not change."
    )

    html_body = render_email(
        title=subject,
        preheader="Choose a new password. This link expires in 30 minutes.",
        heading="Reset your password",
        blocks=[
            paragraph(
                "We received a request to reset the password for your "
                "BridgeLeads account. Use the button below to choose a new one."
            ),
        ],
        cta=("Reset Password", reset_link),
        cta_note=(
            "This link expires in 30 minutes and can be used once. For your "
            "security, resetting your password signs you out of all devices."
        ),
        footer_note=footer_note,
    )

    text_body = (
        "We received a request to reset your BridgeLeads password.\n\n"
        f"Reset your password: {reset_link}\n\n"
        "This link expires in 30 minutes and can be used once.\n"
        "For your security, resetting your password signs you out of all devices.\n\n"
        + text_footer(footer_note=footer_note)
    )

    try:
        resend.Emails.send(build_payload(
            to=[email], subject=subject, html_body=html_body, text_body=text_body,
        ))
        _logger.info("Password reset email sent to %s", email)
    except Exception as exc:
        _logger.error("Failed to send password reset email to %s: %s", email, exc)
