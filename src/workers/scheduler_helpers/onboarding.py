"""Body logic for the send_onboarding_emails beat task.

Each email is claimed before it is sent, so a duplicate run sends nothing new.
Duplicates are real: during a deploy two beat containers can briefly coexist and
publish the same daily entry twice, and a worker can be handed a task again
after it is lost. The claim is a Redis marker per user and email (for the
trial-ending warning, per countdown step), set atomically with NX and kept for
30 days, so the second run finds it and skips. It is at-most-once by design:
a crash between the claim and the send loses that one email, which beats sending
marketing twice. Redis is not a durable ledger, so a marker lost to a flush or a
failover can let a later duplicate run through; that rare case is accepted.
"""

import uuid
from datetime import UTC, datetime

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

_CELERY_TIME_LIMITS = (SoftTimeLimitExceeded, TimeLimitExceeded)

# Longer than any eligibility window (day 1, day 3, the last two trial days), so a
# late redelivery still finds the marker; the task recomputes eligibility anyway.
_CLAIM_TTL_S = 30 * 24 * 3600
_REDIS_TIMEOUT_S = 5

# Outcomes where nothing reached Resend, so the email may be tried again. FAILED
# is not one: the Resend call may have been delivered before it errored.
_RETRYABLE = frozenset({"not_sent", "skipped", "held"})

# Deletes the marker only if it is still this run's claim.
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""

UNCLAIMED = "unclaimed"   # Redis could not be asked, so nothing was sent (fails closed)
DUPLICATE = "duplicate"   # another run already claimed this email


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


def _naive_utc(value: datetime) -> datetime:
    """A naive UTC datetime; an aware value is converted to UTC first."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _claim_key(kind: str, user_id, *step) -> str:
    env = settings.ENVIRONMENT.strip().lower() or "unknown"
    return ":".join(["bl", env, "onboarding_email", kind, str(user_id), *map(str, step)])


def _redis_client():
    import redis as sync_redis

    return sync_redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=_REDIS_TIMEOUT_S,
        socket_timeout=_REDIS_TIMEOUT_S,
        **settings.redis_kwargs(),
    )


def _send_once(client, key: str, send) -> str:
    """Claim ``key``, then run ``send()`` and return its outcome.

    Returns DUPLICATE when another run holds the claim and UNCLAIMED when Redis
    cannot be asked (nothing is sent either way). After the send the claim is
    kept, except when nothing reached Resend; an exception from ``send()`` keeps
    it too, since the email may have gone out.
    """
    token = uuid.uuid4().hex
    try:
        claimed = client.set(key, token, nx=True, ex=_CLAIM_TTL_S)
    except _CELERY_TIME_LIMITS:
        raise
    except Exception as exc:  # noqa: BLE001 -- fail closed: no claim, no email
        _logger.warning("Onboarding email claim unavailable: %s", type(exc).__name__)
        return UNCLAIMED
    if not claimed:
        return DUPLICATE
    outcome = send()
    if outcome in _RETRYABLE:
        try:
            client.eval(_RELEASE_IF_OWNER, 1, key, token)
        except _CELERY_TIME_LIMITS:
            raise
        except Exception as exc:  # noqa: BLE001 -- the email is skipped until the TTL ends
            _logger.warning("Onboarding email claim not released: %s", type(exc).__name__)
    return outcome


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

    client = _redis_client()
    try:
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

                # Converted to UTC before the zone is dropped: a database session
                # in another time zone returns local times, and stripping those
                # shifted every day count by the UTC offset.
                trial_end = _naive_utc(user.trial_ends_at)
                now_naive = now.replace(tzinfo=None)
                days_since_signup = (now_naive - _naive_utc(user.created_at)).days
                days_left = (trial_end - now_naive).days

                # Sprint 5.3 Day 1: nudge if the user still hasn't created a
                # scraper 24 hours after signup. The beat runs daily, so
                # days_since_signup==1 matches one run; the claim stops a
                # duplicate run from sending it again.
                if days_since_signup == 1:
                    has_scraper = _exists(db, ScraperConfig.user_id == user.id)
                    if not has_scraper:
                        # days_left is this user's REAL remaining trial, not a
                        # literal. The copy used to always read "6 more days".
                        outcome = _send_once(
                            client, _claim_key("day1", user.id),
                            lambda u=user, d=days_left: send_day1_nudge(u.email, d),
                        )
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

                    outcome = _send_once(
                        client, _claim_key("day3", user.id),
                        lambda u=user, s=has_scraper, h=has_download, d=days_left:
                            send_activation_reminder(u.email, s, h, d),
                    )
                    outcomes[outcome] += 1
                    day3_sent += outcome == SENT

                # Day 6 or 7: trial expiry warning, one per countdown step. The
                # trial end is part of the key, so a new trial end starts a new
                # countdown.
                if days_left in (1, 2):
                    trial_end_key = trial_end.strftime("%Y%m%dT%H%M%S")
                    outcome = _send_once(
                        client, _claim_key("trial_ending", user.id, trial_end_key, days_left),
                        lambda u=user, d=days_left: send_trial_ending_email(u.email, d),
                    )
                    outcomes[outcome] += 1
                    expiry_sent += outcome == SENT
    finally:
        client.close()

    _logger.info(
        "Onboarding email check: %d trial users evaluated (sent: day1=%d day3=%d "
        "expiry=%d; outcomes=%s)",
        len(users), day1_sent, day3_sent, expiry_sent, dict(outcomes),
    )
