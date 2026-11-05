"""Body logic for the send_onboarding_emails beat task."""

from datetime import UTC, datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _exists(db, *conditions) -> bool:
    """True if any row matches.

    Not `select(Model).where(...).scalar_one_or_none() is not None`, which is what
    this module used to ask: SQLAlchemy raises MultipleResultsFound the moment a
    user has two scrapers, and because this runs inside the daily beat loop the
    exception aborted the whole task, silently costing every LATER user their
    onboarding emails too.
    """
    from sqlalchemy import select

    return db.execute(select(1).where(*conditions).limit(1)).first() is not None


def _send_onboarding_emails_impl() -> None:
    """Send day-1 nudge, day-3 activation reminder, day 6-7 trial expiry warnings."""
    from collections import Counter

    from sqlalchemy import select

    from src.db.models import ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.workers.onboarding_emails import (
        SENT,
        send_activation_reminder,
        send_day1_nudge,
        send_trial_ending_email,
    )

    now = datetime.now(UTC)
    day1_sent = 0
    day3_sent = 0
    expiry_sent = 0
    # Every outcome, not just sends: a run where everything was HELD (no postal
    # address configured) must not log as if the emails went out.
    outcomes: Counter[str] = Counter()

    with SyncSessionLocal() as db:
        # deletion_state IS NULL: an account that asked to be deleted keeps
        # is_active until its purge is claimed, and must get no more marketing.
        # (Opt-outs are checked per address at send time, in the senders.)
        users = db.execute(
            select(User).where(
                User.is_active,
                User.deletion_state.is_(None),
                User.trial_ends_at.isnot(None),
            )
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
                has_scraper = _exists(db, ScraperConfig.user_id == user.id)
                if not has_scraper:
                    # days_left is this user's REAL remaining trial, not a
                    # literal. The copy used to always read "6 more days".
                    outcome = send_day1_nudge(user.email, days_left)
                    outcomes[outcome] += 1
                    day1_sent += outcome == SENT

            # Day 3: activation nudge (scraper exists but no downloads yet)
            if days_since_signup == 3:
                has_scraper = _exists(db, ScraperConfig.user_id == user.id)

                # Observed download (migration 090), not "an export exists". The
                # old predicate read jobs.export_key, which the worker writes
                # when it marks a job DONE, so this email told users who had
                # never downloaded that they were already activated.
                has_download = (
                    user.first_leads_downloaded_at is not None
                    or bool(user.onboarding_download_grandfathered)
                )

                outcome = send_activation_reminder(
                    user.email, has_scraper, has_download, days_left
                )
                outcomes[outcome] += 1
                day3_sent += outcome == SENT

            # Day 6 or 7: trial expiry warning
            if days_left in (1, 2):
                outcome = send_trial_ending_email(user.email, days_left)
                outcomes[outcome] += 1
                expiry_sent += outcome == SENT

    _logger.info(
        "Onboarding email check: %d trial users evaluated (sent: day1=%d day3=%d "
        "expiry=%d; outcomes=%s)",
        len(users), day1_sent, day3_sent, expiry_sent, dict(outcomes),
    )
