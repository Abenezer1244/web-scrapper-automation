"""Email delivery: send job results via Resend after successful export."""

import html

import resend

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.delivery")

resend.api_key = settings.RESEND_API_KEY


def deliver_job_results(
    job_id: str,
    scraper_name: str,
    record_count: int,
    download_url: str,
    recipient_emails: list[str],
    fmt: str = "csv",
) -> None:
    """Send a lead delivery email via Resend.

    Called by the Celery worker after a successful export upload to R2.

    Args:
        job_id: The job UUID (for reference in subject line).
        scraper_name: Human-readable scraper name (e.g. 'Pierce County Probate').
        record_count: Number of records in the export.
        download_url: Pre-signed R2 URL (48hr expiry for email delivery).
        recipient_emails: List of email addresses to send to.
        fmt: Export format label shown in email ('csv', 'excel', 'json').
    """
    if not recipient_emails:
        _logger.info("No delivery emails configured for job %s — skipping", job_id)
        return

    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not configured — skipping email delivery for job %s", job_id)
        return

    safe_name = html.escape(scraper_name)
    subject = f"Your {scraper_name} leads are ready — {record_count:,} records"

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

    <a href="{html.escape(download_url)}" class="btn">Download {fmt.upper()}</a>

    <p class="expiry">This download link expires in 48 hours.</p>

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
        f"Download ({fmt.upper()}): {download_url}\n\n"
        "This link expires in 48 hours.\n"
        "Manage delivery settings at app.bridgeleads.io"
    )

    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": recipient_emails,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
        _logger.info(
            "Delivery email sent for job %s to %d recipients (%d records)",
            job_id, len(recipient_emails), record_count,
        )
    except Exception as exc:
        # Log but don't raise — a failed email must not fail the job
        _logger.error("Failed to send delivery email for job %s: %s", job_id, exc)


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
