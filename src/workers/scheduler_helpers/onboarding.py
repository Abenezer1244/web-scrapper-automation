"""Body logic for the send_onboarding_emails beat task."""

from datetime import UTC, datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _send_onboarding_emails_impl() -> None:
    """Send day-1 nudge, day-3 activation reminder, day 6-7 trial expiry warnings."""
    from sqlalchemy import select

    from src.db.models import Job, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.workers.onboarding_emails import (
        send_activation_reminder,
        send_day1_nudge,
        send_trial_ending_email,
    )

    now = datetime.now(UTC)
    day1_sent = 0
    day3_sent = 0
    expiry_sent = 0

    with SyncSessionLocal() as db:
        users = db.execute(
            select(User).where(User.is_active, User.trial_ends_at.isnot(None))
        ).scalars().all()

        for user in users:
            if not user.trial_ends_at:
                continue

            trial_end = user.trial_ends_at.replace(tzinfo=None) if user.trial_ends_at.tzinfo else user.trial_ends_at
            now_naive = now.replace(tzinfo=None)
            days_since_signup = (now_naive - user.created_at.replace(tzinfo=None)).days
            days_left = (trial_end - now_naive).days

            # Sprint 5.3 Day 1: nudge if the user still hasn't created
            # a scraper 24 hours after signup. Only runs once (this beat
            # task runs daily so days_since_signup==1 matches a ~24h window).
            if days_since_signup == 1:
                has_scraper = db.execute(
                    select(ScraperConfig).where(ScraperConfig.user_id == user.id)
                ).scalar_one_or_none() is not None
                if not has_scraper:
                    send_day1_nudge(user.email)
                    day1_sent += 1

            # Day 3: activation nudge (scraper exists but no downloads yet)
            if days_since_signup == 3:
                has_scraper = db.execute(
                    select(ScraperConfig).where(ScraperConfig.user_id == user.id)
                ).scalar_one_or_none() is not None

                has_download = db.execute(
                    select(Job).where(Job.user_id == user.id, Job.export_key.isnot(None))
                ).scalar_one_or_none() is not None

                send_activation_reminder(user.email, has_scraper, has_download)
                day3_sent += 1

            # Day 6 or 7: trial expiry warning
            if days_left in (1, 2):
                send_trial_ending_email(user.email, days_left)
                expiry_sent += 1

    _logger.info(
        "Onboarding email check: %d trial users evaluated (day1=%d day3=%d expiry=%d)",
        len(users), day1_sent, day3_sent, expiry_sent,
    )
