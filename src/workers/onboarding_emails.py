"""Onboarding email sequence: welcome, duplicate signup, nudges, trial expiry.

All five templates render through src/utils/email_layout.py, which owns the
sender identity (display name + Reply-To) and the email-safe HTML shell.

Every plan number in this module is READ from config, never written here. The
trial-expiry email used to hardcode "Pro ($79/mo)" long after billing moved Pro
to $199, quoting customers a price we do not charge. Prices and record
allowances come from src/config/plans.py (the same catalog /billing/plans
serves) and the trial length from constants.TRIAL_PERIOD_DAYS (the same value
registration stamps on trial_ends_at).
"""

import resend

from src.config import settings
from src.config.constants import TRIAL_PERIOD_DAYS
from src.config.plans import format_price_monthly, get_plan, plan_records_limit
from src.utils.email_layout import (
    build_payload,
    bullets,
    callout,
    numbered_steps,
    paragraph,
    render_email,
    text_footer,
)
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.onboarding")

resend.api_key = settings.RESEND_API_KEY


def _send(email: str, subject: str, html_body: str, text_body: str) -> None:
    if not settings.RESEND_API_KEY:
        _logger.warning("RESEND_API_KEY not set, skipping email to %s", email)
        return
    try:
        resend.Emails.send(build_payload(
            to=[email], subject=subject, html_body=html_body, text_body=text_body,
        ))
        _logger.info("Sent '%s' to %s", subject, email)
    except Exception as exc:
        _logger.error("Failed to send '%s' to %s: %s", subject, email, exc)


def _days_phrase(days: int) -> str:
    """'today', '1 day' or 'N days' for trial-countdown copy."""
    if days <= 0:
        return "today"
    return "1 day" if days == 1 else f"{days} days"


# ─── Day 0: Welcome ─────────────────────────────────────────────────────────

def send_welcome_email(email: str) -> None:
    """Sent immediately after registration.

    Deliberately generic: the steps name no specific county or record type,
    because the counties and record types a given recipient can reach depend on
    their plan and on which connectors are live. The old copy told everyone to
    pick Pierce or King and to choose probate.
    """
    url = f"{settings.FRONTEND_URL}/scrapers/new"
    plan = get_plan("pro")
    records = plan_records_limit("pro")
    trial_days = TRIAL_PERIOD_DAYS

    subject = "Welcome to BridgeLeads. Get your first property records in minutes"
    trial_line = (
        f"Your free {plan['name']} trial runs for {trial_days} days and includes "
        f"{records:,} records per month. No credit card required."
    )

    html_body = render_email(
        title=subject,
        preheader="Set up your first scraper and export county records.",
        heading="Welcome to BridgeLeads",
        blocks=[
            paragraph(
                "Your account is ready. Here is how to pull your first list of "
                "property records."
            ),
            numbered_steps([
                ("Choose a county",
                 "Pick any county your plan covers from the scraper setup screen."),
                ("Choose a record type",
                 "Probate, pre-foreclosure, tax delinquent and the other lists "
                 "available on your plan."),
                ("Run your scraper",
                 "Records are pulled live from the county source and appear as "
                 "they are found."),
                ("Review or export your results",
                 "Export to CSV or Excel. Property and mailing addresses are "
                 "included where the county publishes them."),
            ]),
            callout(trial_line),
        ],
        cta=("Set Up Your First Scraper", url),
    )

    text_body = (
        "Welcome to BridgeLeads\n\n"
        "Your account is ready. Here is how to pull your first list of property "
        "records.\n\n"
        "1. Choose a county. Pick any county your plan covers.\n"
        "2. Choose a record type. Probate, pre-foreclosure, tax delinquent and "
        "the other lists available on your plan.\n"
        "3. Run your scraper. Records are pulled live from the county source.\n"
        "4. Review or export your results. Export to CSV or Excel with property "
        "and mailing addresses.\n\n"
        f"Set up your first scraper: {url}\n\n"
        f"{trial_line}\n\n"
        f"{text_footer()}"
    )

    _send(email, subject, html_body, text_body)


# ─── Duplicate signup: "you already have an account" ───────────────────────

@app.task(name="src.workers.onboarding_emails.send_duplicate_signup_email")
def send_duplicate_signup_email(email: str) -> None:
    """Sent when someone submits /auth/register for an address that ALREADY has
    an account (the duplicate-email branch returns a generic 400 to the client to
    avoid user enumeration; this out-of-band note to the inbox owner is how a
    legitimate returning user learns what to do instead).

    Run as a Celery task (off the request path) so it adds NO latency to the
    register response, keeping the response time of an existing-email attempt
    indistinguishable from a new-email attempt (no timing/enumeration oracle).
    The caller gates this to at most once per address per 24h.

    The copy intentionally does not over-confirm: it states only that a signup
    was ATTEMPTED for this address (which the inbox owner can see anyway) and
    offers login + reset. It does not echo any other account detail.
    """
    login_url = f"{settings.FRONTEND_URL}/login"
    reset_url = f"{settings.FRONTEND_URL}/forgot-password"
    subject = "You already have a BridgeLeads account"

    html_body = render_email(
        title=subject,
        preheader="Log in instead, or reset your password.",
        heading="You already have an account",
        blocks=[
            paragraph(
                "Someone just tried to create a BridgeLeads account with this "
                "email address, but one already exists. If that was you, there "
                "is no need to sign up again. Just log in."
            ),
            paragraph(f"Forgot your password? Reset it at {reset_url}", muted=True),
        ],
        cta=("Log In", login_url),
        footer_note=(
            "If this was not you, you can safely ignore this email. No account "
            "changes were made."
        ),
    )

    text_body = (
        "You already have a BridgeLeads account.\n\n"
        "Someone just tried to create an account with this email address, but one "
        "already exists. If that was you, there is no need to sign up again. "
        "Just log in.\n\n"
        f"Log in: {login_url}\n"
        f"Reset your password: {reset_url}\n\n"
        + text_footer(footer_note=(
            "If this was not you, you can safely ignore this email. No account "
            "changes were made."
        ))
    )

    _send(email, subject, html_body, text_body)


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
    subject = "Confirm your email to finish signing up"
    trial_days = TRIAL_PERIOD_DAYS
    footer_note = (
        "If you did not try to sign up for BridgeLeads, you can safely ignore "
        "this email. No account was created."
    )

    html_body = render_email(
        title=subject,
        preheader="Confirm your email address to finish creating your account.",
        heading="Confirm your email",
        blocks=[
            paragraph(
                f"You are one click away from your BridgeLeads account. Confirm "
                f"this email address to finish signing up and start your "
                f"{trial_days}-day free Pro trial."
            ),
        ],
        cta=("Confirm Email and Start Trial", verify_link),
        cta_note="This link expires in 24 hours.",
        footer_note=footer_note,
    )

    text_body = (
        "Confirm your email to finish signing up for BridgeLeads.\n\n"
        f"Confirm your address and start your {trial_days}-day free Pro trial:\n"
        f"{verify_link}\n\n"
        "This link expires in 24 hours.\n\n"
        + text_footer(footer_note=footer_note)
    )
    # Direct send (NOT via _send) so any Resend/network error PROPAGATES to the
    # dispatcher, which classifies it (retryable -> backoff + retry; permanent ->
    # mark the row 'failed' + ops-alert) instead of swallowing it. It still uses
    # build_payload so the From display name and Reply-To stay centralised.
    resend.Emails.send(build_payload(
        to=[email], subject=subject, html_body=html_body, text_body=text_body,
    ))
    _logger.info("Sent verification email to %s", email)


# ─── Day 1: "Having trouble getting started?" ──────────────────────────────

def send_day1_nudge(email: str, days_left: int) -> None:
    """Sent on day 1 to users who haven't created a scraper yet.

    Different from the welcome email (day 0): this one acknowledges the gap and
    offers help. Escalation sequence:
      Day 0: Welcome (send_welcome_email)
      Day 1: Day-1 nudge (THIS)                only if zero scrapers
      Day 3: Activation reminder               only if no download
      Day 6-7: Trial expiry warning            only if trial user

    ``days_left`` is the caller's real remaining trial days, not a literal. The
    old copy always read "6 more days" regardless of the account's actual state.
    """
    url = f"{settings.FRONTEND_URL}/dashboard"
    subject = "Getting started with your first scrape"
    trial_line = (
        f"Your free trial has {_days_phrase(days_left)} left."
        if days_left > 0 else "Your free trial ends today."
    )

    html_body = render_email(
        title=subject,
        preheader="Your dashboard has a one click option to run your first scrape.",
        heading="Ready to run your first scrape?",
        blocks=[
            paragraph(
                "You signed up yesterday and have not run a scrape yet. Your "
                "dashboard has a one click option that sets up a scraper and "
                "runs it for you."
            ),
            callout(trial_line),
        ],
        cta=("Go to Dashboard", url),
    )

    text_body = (
        "Ready to run your first scrape?\n\n"
        "You signed up yesterday and have not run a scrape yet. Your dashboard "
        "has a one click option that sets up a scraper and runs it for you.\n\n"
        f"Go to your dashboard: {url}\n\n"
        f"{trial_line}\n\n"
        f"{text_footer()}"
    )

    _send(email, subject, html_body, text_body)


# ─── Day 3: Activation nudge ────────────────────────────────────────────────

def send_activation_reminder(
    email: str, has_scraper: bool, has_download: bool, days_left: int
) -> None:
    """Sent on day 3 if user hasn't completed activation."""
    if has_download:
        return  # Already activated

    if not has_scraper:
        subject = "You have not set up a scraper yet"
        heading = "Set up your first scraper"
        message = (
            "Creating a scraper takes about a minute. Choose a county and a "
            "record type, and BridgeLeads pulls the records for you."
        )
        cta_label = "Set Up a Scraper"
        url = f"{settings.FRONTEND_URL}/scrapers/new"
    else:
        subject = "Your records are ready to export"
        heading = "Your records are ready to export"
        message = (
            "You ran a scrape but have not exported the results yet. Records "
            "are most useful while they are fresh."
        )
        cta_label = "View Your Results"
        url = f"{settings.FRONTEND_URL}/results"

    trial_line = (
        f"Your free trial has {_days_phrase(days_left)} left."
        if days_left > 0 else "Your free trial ends today."
    )

    html_body = render_email(
        title=subject,
        preheader=message,
        heading=heading,
        blocks=[paragraph(message), callout(trial_line)],
        cta=(cta_label, url),
    )

    text_body = (
        f"{heading}\n\n{message}\n\n"
        f"{cta_label}: {url}\n\n"
        f"{trial_line}\n\n"
        f"{text_footer()}"
    )

    _send(email, subject, html_body, text_body)


# ─── Day 6-7: Trial expiry ──────────────────────────────────────────────────

def send_trial_ending_email(email: str, days_left: int) -> None:
    """Sent when trial has 1-2 days remaining.

    Everything factual here is read from config: the Pro price and feature list
    from the billing plan catalog, and the post-trial allowance from the same
    Starter limit that _expire_trials_impl actually applies when it downgrades
    the account. No county cap is quoted, because per-tier county gating is not
    enforced (settings.ENTITLEMENT_ENFORCEMENT is off) and the billing catalog
    deliberately does not advertise one either.
    """
    pro = get_plan("pro")
    starter_records = plan_records_limit("starter")
    price = format_price_monthly("pro")

    if days_left <= 1:
        subject = f"Your BridgeLeads {pro['name']} trial ends today"
        heading = f"Your {pro['name']} trial ends today"
    else:
        subject = f"Your BridgeLeads {pro['name']} trial ends in {days_left} days"
        heading = f"Your {pro['name']} trial ends in {days_left} days"

    url = f"{settings.FRONTEND_URL}/settings?tab=billing"
    after_trial = (
        f"When your trial ends, your account moves to the Starter plan, which "
        f"includes {starter_records:,} records per month. Your account, scrapers "
        f"and past results stay in place."
    )
    price_line = f"{pro['name']} is {price} and includes:"

    html_body = render_email(
        title=subject,
        preheader=f"Upgrade to keep {pro['name']} access. {price}.",
        heading=heading,
        blocks=[
            paragraph(f"Your BridgeLeads {pro['name']} trial is almost over."),
            callout(after_trial, tone="warning"),
            paragraph(price_line),
            bullets(list(pro["features"])[:5]),
        ],
        cta=(f"Upgrade to {pro['name']}", url),
        cta_note="You can cancel at any time from your billing settings.",
    )

    text_body = (
        f"{heading}\n\n"
        f"Your BridgeLeads {pro['name']} trial is almost over.\n\n"
        f"{after_trial}\n\n"
        f"{price_line}\n"
        + "".join(f"  - {f}\n" for f in list(pro["features"])[:5])
        + f"\nUpgrade: {url}\n\n"
        "You can cancel at any time from your billing settings.\n\n"
        f"{text_footer()}"
    )

    _send(email, subject, html_body, text_body)


__all__ = [
    "send_activation_reminder",
    "send_day1_nudge",
    "send_duplicate_signup_email",
    "send_trial_ending_email",
    "send_welcome_email",
]
