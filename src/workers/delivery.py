"""Email delivery: send job results via Resend after successful export."""

import html

import requests
import resend

from src.config import settings
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

    Retry: network/transport errors (requests.RequestException) and server-side
    Resend errors whose HTTP status is 408/409/429 or 5xx. Do NOT retry permanent
    client errors (auth, validation, malformed payload) — they fail identically
    on every attempt and just waste the retry budget.
    """
    if isinstance(exc, requests.RequestException):
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
    safe_name = html.escape(scraper_name)
    subject = f"Your {scraper_name} leads are ready — {record_count:,} records"

    # DNC/TCPA disclaimer is shown HERE (and the download UI), not inside the CSV
    # — a disclaimer row corrupts a dialer/spreadsheet import. Single source in
    # constants so every surface shows identical copy.
    from src.config.constants import DNC_DISCLAIMER
    safe_disclaimer = html.escape(DNC_DISCLAIMER)

    # Batch deliveries link to the in-app batch page (no expiry); per-job links
    # are 48h presigns. Wrong copy on a batch email erodes trust (Codex P2).
    # Fragments carry their own full line including indent + trailing newline,
    # or empty string — so when empty, no stray whitespace line in the template.
    expiry_html = (
        "    <p class=\"expiry\">This download link expires in 48 hours.</p>\n"
        if link_expires else ""
    )
    expiry_text = "This link expires in 48 hours.\n\n" if link_expires else ""
    summary_html = (
        f"    <p class=\"meta\" style=\"margin-top:-16px; margin-bottom:24px;\">"
        f"{html.escape(summary_message)}</p>\n"
        if summary_message else ""
    )
    summary_text = f"{summary_message}\n\n" if summary_message else ""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
    .card {{ background: #111113; border: 1px solid #2a2a32; border-radius: 12px; max-width: 520px; margin: 0 auto; padding: 36px; }}
    .logo {{ font-size: 18px; font-weight: 600; color: #f5a623; margin-bottom: 28px; }}
    h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 8px; }}
    .meta {{ color: #9998a0; font-size: 13px; margin-bottom: 28px; }}
    .stat {{ background: #1a1208; border: 1px solid #7a4f08; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; }}
    .stat-number {{ font-size: 36px; font-weight: 700; color: #f5a623; line-height: 1; }}
    .stat-label {{ font-size: 12px; color: #9998a0; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 4px; }}
    .btn {{ display: inline-block; background: #f5a623; color: #0a0a0b; font-weight: 600; font-size: 15px; padding: 14px 28px; border-radius: 8px; text-decoration: none; margin-bottom: 24px; }}
    .footer {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
    .expiry {{ font-size: 12px; color: #55545e; margin-top: 12px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">BridgeLeads</div>
    <h1>Your leads are ready</h1>
    <p class="meta">{safe_name}</p>

    <div class="stat">
      <div class="stat-number">{record_count:,}</div>
      <div class="stat-label">Records found</div>
    </div>

{summary_html}    <a href="{html.escape(download_url)}" class="btn">Download {fmt.upper()}</a>

{expiry_html}
    <div class="notice" style="font-size:12px; color:#d9b13a; background:#1a1208; border:1px solid #7a4f08; border-radius:8px; padding:12px 16px; margin-bottom:20px; line-height:1.5;">
      {safe_disclaimer}
    </div>

    <div class="footer">
      You're receiving this because you set up automated delivery for {safe_name}.<br>
      Manage your delivery settings at app.bridgeleads.io
    </div>
  </div>
</body>
</html>
"""

    text_body = (
        f"Your {scraper_name} leads are ready.\n\n"
        f"{record_count:,} records found.\n\n"
        f"{summary_text}"
        f"Download ({fmt.upper()}): {download_url}\n\n"
        f"{expiry_text}"
        f"{DNC_DISCLAIMER}\n\n"
        "Manage delivery settings at app.bridgeleads.io"
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
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": recipient_emails,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
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

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
    .card {{ background: #111113; border: 1px solid #2a2a32; border-radius: 12px; max-width: 520px; margin: 0 auto; padding: 36px; }}
    .logo {{ font-size: 18px; font-weight: 600; color: #f5a623; margin-bottom: 28px; }}
    h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 8px; }}
    .alert {{ background: #1a0808; border: 1px solid #7a0808; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; color: #f87171; font-size: 14px; }}
    .btn {{ display: inline-block; background: #f5a623; color: #0a0a0b; font-weight: 600; font-size: 15px; padding: 14px 28px; border-radius: 8px; text-decoration: none; margin-bottom: 24px; }}
    .footer {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">BridgeLeads</div>
    <h1>Payment failed</h1>

    <div class="alert">
      This is the {ordinal} failed payment attempt. Please update your payment method to keep your subscription active.
    </div>

    <a href="{settings.FRONTEND_URL}/settings?tab=billing" class="btn">Update Payment Method</a>

    <div class="footer">
      If you believe this is an error, contact us at support@bridgeleads.io<br>
      Manage your subscription at app.bridgeleads.io
    </div>
  </div>
</body>
</html>
"""

    text_body = (
        f"Your BridgeLeads payment failed (attempt {attempt_count}).\n\n"
        "Please update your payment method to keep your subscription active.\n\n"
        f"Update payment: {settings.FRONTEND_URL}/settings?tab=billing\n\n"
        "If you believe this is an error, contact support@bridgeleads.io"
    )

    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
        _logger.info("Payment failed email sent to %s (attempt %d)", email, attempt_count)
    except Exception as exc:
        _logger.error("Failed to send payment failed email to %s: %s", email, exc)


def send_lockout_notification(email: str, failure_count: int, ip: str) -> None:
    """Notify a user that their account is under a brute-force attack.

    Soft-fails — never raises so auth flow is not disrupted.
    """
    if not settings.RESEND_API_KEY:
        return

    safe_ip = html.escape(ip)  # email is the recipient, never rendered in the body
    subject = "Security alert: suspicious login activity on your BridgeLeads account"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
    .card {{ background: #111113; border: 1px solid #2a2a32; border-radius: 12px; max-width: 520px; margin: 0 auto; padding: 36px; }}
    .logo {{ font-size: 18px; font-weight: 600; color: #f5a623; margin-bottom: 28px; }}
    h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 8px; }}
    .alert {{ background: #1a0808; border: 1px solid #7a0808; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; color: #f87171; font-size: 14px; }}
    .footer {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">BridgeLeads</div>
    <h1>Suspicious login activity</h1>

    <div class="alert">
      We detected {failure_count} failed login attempts on your account from IP {safe_ip}.
      Your account has been temporarily locked for your protection.
    </div>

    <p>If this was you, wait a few minutes and try again. If you didn't attempt to log in,
    we recommend changing your password immediately.</p>

    <div class="footer">
      This is an automated security notification from BridgeLeads.<br>
      Contact support@bridgeleads.io if you need help.
    </div>
  </div>
</body>
</html>
"""

    text_body = (
        f"Security alert: {failure_count} failed login attempts detected on your "
        f"BridgeLeads account from IP {ip}.\n\n"
        "Your account has been temporarily locked.\n\n"
        "If this wasn't you, change your password immediately.\n"
        "Contact support@bridgeleads.io if you need help."
    )

    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
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

    # reset_link is a server-built FRONTEND_URL + signed token — escape
    # it anyway so it is never an HTML-injection vector in the markup.
    safe_link = html.escape(reset_link)
    subject = "Reset your BridgeLeads password"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
    .card {{ background: #111113; border: 1px solid #2a2a32; border-radius: 12px; max-width: 520px; margin: 0 auto; padding: 36px; }}
    .logo {{ font-size: 18px; font-weight: 600; color: #f5a623; margin-bottom: 28px; }}
    h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 8px; }}
    p {{ font-size: 14px; line-height: 1.5; color: #c9c8d0; }}
    .btn {{ display: inline-block; background: #f5a623; color: #0a0a0b; font-weight: 600; font-size: 15px; padding: 14px 28px; border-radius: 8px; text-decoration: none; margin: 16px 0 24px; }}
    .expiry {{ font-size: 12px; color: #55545e; margin-top: 12px; }}
    .footer {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">BridgeLeads</div>
    <h1>Reset your password</h1>

    <p>We received a request to reset the password for your BridgeLeads account.
    Click the button below to choose a new one.</p>

    <a href="{safe_link}" class="btn">Reset Password</a>

    <p class="expiry">This link expires in 30 minutes and can be used once.
    For your security, resetting your password signs you out of all devices.</p>

    <div class="footer">
      If you didn't request this, you can safely ignore this email — your
      password won't change.<br>
      Contact support@bridgeleads.io if you need help.
    </div>
  </div>
</body>
</html>
"""

    text_body = (
        "We received a request to reset your BridgeLeads password.\n\n"
        f"Reset your password: {reset_link}\n\n"
        "This link expires in 30 minutes and can be used once.\n"
        "For your security, resetting your password signs you out of all devices.\n\n"
        "If you didn't request this, you can safely ignore this email.\n"
        "Contact support@bridgeleads.io if you need help."
    )

    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
        _logger.info("Password reset email sent to %s", email)
    except Exception as exc:
        _logger.error("Failed to send password reset email to %s: %s", email, exc)
