"""Onboarding email sequence: welcome, activation nudge, trial expiry."""

import html

import resend

from src.config import settings
from src.utils.logger import setup_logger
from src.workers import app

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
  <p style="font-size: 13px; color: #9998a0;">7-day free Pro trial. 1,000 records/month. No credit card.</p>
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


# ─── Duplicate signup: "you already have an account" ───────────────────────

@app.task(name="src.workers.onboarding_emails.send_duplicate_signup_email")
def send_duplicate_signup_email(email: str) -> None:
    """Sent when someone submits /auth/register for an address that ALREADY has
    an account (the duplicate-email branch returns a generic 400 to the client to
    avoid user enumeration; this out-of-band note to the inbox owner is how a
    legitimate returning user learns what to do instead).

    Run as a Celery task (off the request path) so it adds NO latency to the
    register response — keeping the response time of an existing-email attempt
    indistinguishable from a new-email attempt (no timing/enumeration oracle).
    The caller gates this to at most once per address per 24h.

    The copy intentionally does not over-confirm: it states only that a signup
    was ATTEMPTED for this address (which the inbox owner can see anyway) and
    offers login + reset. It does not echo any other account detail.
    """
    login_url = f"{settings.FRONTEND_URL}/login"
    reset_url = f"{settings.FRONTEND_URL}/forgot-password"
    subject = "You already have a BridgeLeads account"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.btn {{ {_BTN_STYLE} }}
.alt {{ font-size: 14px; color: #c8c7cf; }}
.alt a {{ color: #10b981; }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>Looks like you already have an account</h1>
  <p style="color: #c8c7cf; font-size: 14px;">
    Someone just tried to create a BridgeLeads account with this email address,
    but one already exists. If that was you, there&rsquo;s no need to sign up
    again &mdash; just log in.
  </p>
  <a href="{login_url}" class="btn">Log in</a>
  <p class="alt">Forgot your password? <a href="{reset_url}">Reset it here</a>.</p>
  <div class="foot">
    If this wasn&rsquo;t you, you can safely ignore this email &mdash; no account
    changes were made.
  </div>
</div></body></html>"""

    _send(email, subject, html_body, (
        "Looks like you already have a BridgeLeads account.\n\n"
        "Someone just tried to create an account with this email address, but one\n"
        "already exists. If that was you, there's no need to sign up again - just log in.\n\n"
        f"Log in: {login_url}\n"
        f"Forgot your password? Reset it here: {reset_url}\n\n"
        "If this wasn't you, you can safely ignore this email - no account changes were made."
    ))


# ─── Email verification: "confirm your email to finish signing up" ─────────

def send_verification_email(email: str, verify_link: str) -> None:
    """Send the verification email — RAISES on failure (no swallow).

    Called INLINE by the `dispatch_pending_verification_emails` beat (NOT via
    .delay from the request path), which owns retry/backoff and records the
    outcome on the pending_registrations row. Unlike the fire-and-forget
    onboarding emails that go through `_send`, this MUST raise so a transient
    Resend/network failure is retried (the beat reclaims the row after its
    backoff) instead of being silently marked delivered and stranding the
    signup. The caller checks settings.RESEND_API_KEY before calling, so a
    missing key never reaches here.

    Carries the single-use, ~24h verification link that, when clicked, creates
    the real account (POST /auth/verify-email) and logs the user in.
    """
    safe_link = html.escape(verify_link, quote=True)
    subject = "Confirm your email to finish signing up"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.btn {{ {_BTN_STYLE} }}
.alt {{ font-size: 13px; color: #9998a0; word-break: break-all; }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>Confirm your email</h1>
  <p style="color: #c8c7cf; font-size: 14px;">
    You&rsquo;re one click away from your BridgeLeads account. Confirm this email
    address to finish signing up and start your 7-day free Pro trial.
  </p>
  <a href="{safe_link}" class="btn">Confirm email &amp; start trial</a>
  <p class="alt">Or paste this link into your browser:<br>{safe_link}</p>
  <div class="foot">
    This link expires in 24 hours. If you didn&rsquo;t try to sign up for
    BridgeLeads, you can safely ignore this email &mdash; no account was created.
  </div>
</div></body></html>"""

    text_body = (
        "Confirm your email to finish signing up for BridgeLeads.\n\n"
        f"Click to confirm and start your 7-day free Pro trial:\n{verify_link}\n\n"
        "This link expires in 24 hours. If you didn't try to sign up, you can\n"
        "safely ignore this email - no account was created."
    )
    # Direct send (NOT via _send) so any Resend/network error PROPAGATES to the
    # dispatcher, which classifies it (retryable -> backoff + retry; permanent ->
    # mark the row 'failed' + ops-alert) instead of swallowing it.
    resend.Emails.send({
        "from": settings.EMAIL_FROM,
        "to": [email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    })
    _logger.info("Sent verification email to %s", email)


# ─── Day 1: "Having trouble getting started?" ──────────────────────────────

def send_day1_nudge(email: str) -> None:
    """Sent on day 1 to users who haven't created a scraper yet.

    Different from the welcome email (day 0) — this one acknowledges the
    gap and offers help. Escalation sequence:
      Day 0: Welcome (send_welcome_email)
      Day 1: Day-1 nudge (THIS)                — only if zero scrapers
      Day 3: Activation reminder               — only if no download
      Day 6-7: Trial expiry warning            — only if trial user
    """
    url = f"{settings.FRONTEND_URL}/dashboard"
    subject = "Need a hand getting your first leads?"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0a0a0b; color: #f0efe8; margin: 0; padding: 40px 20px; }}
.card {{ {_CARD_STYLE} }}
.logo {{ font-size: 18px; font-weight: 600; color: #10b981; margin-bottom: 28px; }}
h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 12px; }}
.btn {{ {_BTN_STYLE} }}
.hint {{ background: #0f1a15; border: 1px solid #1f3a2e; border-radius: 8px; padding: 16px 20px; margin: 20px 0; color: #c8c7cf; font-size: 13px; }}
.foot {{ font-size: 12px; color: #55545e; border-top: 1px solid #2a2a32; padding-top: 20px; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <div class="logo">BridgeLeads</div>
  <h1>Ready to pull your first leads?</h1>
  <p style="color: #c8c7cf; font-size: 14px;">
    You signed up yesterday &mdash; if you haven&rsquo;t run your first
    scrape yet, we made it a one-click button on your dashboard.
  </p>
  <div class="hint">
    <b style="color: #10b981;">The fastest path:</b> log in, click
    <i>&ldquo;Run first scrape now&rdquo;</i> on your dashboard. We&rsquo;ll
    pull the last 90 days of Pierce County probate records &mdash; our
    highest-enrichment county &mdash; and show them live while they scrape.
  </div>
  <a href="{url}" class="btn">Go to dashboard</a>
  <p style="font-size: 13px; color: #9998a0;">
    Your free Pro trial is active for 6 more days &mdash; 1,000 records/month,
    5 counties, daily auto-scrape.
  </p>
  <div class="foot">
    Stuck on something? Just reply to this email and I&rsquo;ll help directly.
  </div>
</div></body></html>"""

    _send(email, subject, html_body, (
        "Ready to pull your first leads?\n\n"
        "You signed up yesterday. If you haven't run your first scrape yet,\n"
        "we made it a one-click button on your dashboard.\n\n"
        "The fastest path: log in, click 'Run first scrape now'.\n"
        "We'll pull the last 90 days of Pierce County probate records\n"
        "and show them live while they scrape.\n\n"
        f"Go to dashboard: {url}\n\n"
        "Stuck? Reply to this email."
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
  <p style="color: #c8c7cf; font-size: 14px;">Pro ($79/mo) includes: 1,000 records, 5 counties, daily scraping, email delivery.</p>
  <a href="{url}" class="btn">Upgrade to Pro</a>
  <div class="foot">Questions? Reply or contact support@bridgeleads.io</div>
</div></body></html>"""

    _send(email, subject, html_body, f"{headline}\n\nUpgrade: {url}")
