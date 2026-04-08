"""Onboarding email sequence: welcome, activation nudge, trial expiry."""

import html

import resend

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.onboarding")

resend.api_key = settings.RESEND_API_KEY

_CARD_STYLE = (
    "background: #111113; border: 1px solid #2a2a32; border-radius: 12px;"
    " max-width: 520px; margin: 0 auto; padding: 36px;"
)
_BTN_STYLE = (
    "display: inline-block; background: #10b981; color: #0a0a0b;"
    " font-weight: 600; font-size: 15px; padding: 14px 28px;"
    " border-radius: 8px; text-decoration: none; margin: 24px 0;"
)


def _send(email: str, subject: str, html_body: str, text_body: str) -> None:
    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not set, skipping email to %s", email)
        return
    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [email],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        })
        _logger.info("Sent '%s' to %s", subject, email)
    except Exception as exc:
        _logger.error("Failed to send '%s' to %s: %s", subject, email, exc)


# ─── Day 0: Welcome ─────────────────────────────────────────────────────────

def send_welcome_email(email: str) -> None:
    """Sent immediately after registration."""
    url = f"{settings.FRONTEND_URL}/scrapers/new"
    subject = "Welcome to BridgeLeads — get your first leads in 5 minutes"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.step {{ display: flex; gap: 12px; margin-bottom: 14px; }}
.num {{ background: #10b981; color: #0a0a0b; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; flex-shrink: 0; }}
.txt {{ font-size: 14px; color: #c8c7cf; }}
.btn {{ {_BTN_STYLE} }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>Welcome! Get leads in 5 minutes.</h1>
  <div style="margin: 24px 0;">
    <div class="step"><div class="num">1</div><div class="txt"><b>Pick a county</b> — Pierce or King County, WA. 98% address coverage.</div></div>
    <div class="step"><div class="num">2</div><div class="txt"><b>Choose probate</b> — Heirs are the most motivated sellers.</div></div>
    <div class="step"><div class="num">3</div><div class="txt"><b>Click Run</b> — Scraped in real-time. Takes 2-5 minutes.</div></div>
    <div class="step"><div class="num">4</div><div class="txt"><b>Download CSV</b> — Property + mailing address included. Mail letters today.</div></div>
  </div>
  <a href="{url}" class="btn">Set Up Your First Scraper</a>
  <p style="font-size: 13px; color: #9998a0;">7-day free Pro trial. 500 records/month. No credit card.</p>
  <div class="foot">Questions? Reply to this email.</div>
</div></body></html>"""

    _send(email, subject, html_body, (
        "Welcome to BridgeLeads!\n\n"
        "1. Pick a county (Pierce or King, WA)\n"
        "2. Choose probate records\n"
        "3. Click Run (2-5 min)\n"
        "4. Download CSV with addresses\n\n"
        f"Start here: {url}\n\n"
        "7-day free Pro trial. Questions? Reply to this email."
    ))


# ─── Day 3: Activation nudge ────────────────────────────────────────────────

def send_activation_reminder(email: str, has_scraper: bool, has_download: bool) -> None:
    """Sent on day 3 if user hasn't completed activation."""
    if has_download:
        return  # Already activated

    if not has_scraper:
        subject = "You haven't set up your scraper yet"
        msg = "It only takes 30 seconds to create your first scraper."
        cta = "Set Up a Scraper"
        url = f"{settings.FRONTEND_URL}/scrapers/new"
    else:
        subject = "Your leads are waiting"
        msg = "You ran a scrape but haven't downloaded results. Fresh leads lose value fast."
        cta = "Download Your Leads"
        url = f"{settings.FRONTEND_URL}/results"

    safe_msg = html.escape(msg)
    safe_cta = html.escape(cta)

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.btn {{ {_BTN_STYLE} }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>{html.escape(subject)}</h1>
  <p style="color: #c8c7cf; font-size: 14px;">{safe_msg}</p>
  <a href="{url}" class="btn">{safe_cta}</a>
  <p style="font-size: 13px; color: #9998a0;">4 days left on your Pro trial.</p>
  <div class="foot">Need help? Reply to this email.</div>
</div></body></html>"""

    _send(email, subject, html_body, f"{msg}\n\n{cta}: {url}")


# ─── Day 6-7: Trial expiry ──────────────────────────────────────────────────

def send_trial_ending_email(email: str, days_left: int) -> None:
    """Sent when trial has 1-2 days remaining."""
    if days_left <= 1:
        subject = "Your BridgeLeads trial ends today"
        headline = "Your trial ends today"
    else:
        subject = f"Your BridgeLeads trial ends in {days_left} days"
        headline = f"Your trial ends in {days_left} days"

    url = f"{settings.FRONTEND_URL}/settings?tab=billing"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.warn {{ background: #1a1208; border: 1px solid #7a4f08; border-radius: 8px; padding: 16px 20px; margin: 20px 0; color: #f5a623; font-size: 14px; }}
.btn {{ {_BTN_STYLE} }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>{headline}</h1>
  <div class="warn">After your trial, you move to Starter (50 records/month, 1 county). Upgrade to keep Pro access.</div>
  <p style="color: #c8c7cf; font-size: 14px;">Pro ($49/mo) includes: 500 records, 5 counties, daily scraping, email delivery.</p>
  <a href="{url}" class="btn">Upgrade to Pro</a>
  <div class="foot">Questions? Reply or contact support@bridgeleads.io</div>
</div></body></html>"""

    _send(email, subject, html_body, f"{headline}\n\nUpgrade: {url}")
