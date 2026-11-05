"""The onboarding beat sends no commercial email to an account pending deletion.

Real Postgres (no mocks). Compliance audit 2026-11, Phase 4d. An account that has
asked to be deleted keeps is_active until its purge is claimed, so is_active alone
let the day-1 nudge, day-3 reminder and trial-ending upsell keep arriving.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User


def _run_beat_capturing_nudges(monkeypatch) -> list[str]:
    """Run the real beat, recording which addresses it decided to nudge on day 1.

    monkeypatch (pytest's own) only observes the decision; the senders themselves
    are covered in test_email_layout.py.
    """
    from src.workers import onboarding_emails
    from src.workers.scheduler_helpers import onboarding as beat

    nudged: list[str] = []
    monkeypatch.setattr(onboarding_emails, "send_day1_nudge",
                        lambda email, days_left: nudged.append(email) or onboarding_emails.SENT)
    monkeypatch.setattr(onboarding_emails, "send_activation_reminder",
                        lambda *a, **k: onboarding_emails.NOT_NEEDED)
    monkeypatch.setattr(onboarding_emails, "send_trial_ending_email",
                        lambda *a, **k: onboarding_emails.NOT_NEEDED)
    beat._send_onboarding_emails_impl()
    return nudged


async def _make_day1_trial(db: AsyncSession, user: User) -> None:
    user.created_at = datetime.now(UTC) - timedelta(days=1)
    user.trial_ends_at = datetime.now(UTC) + timedelta(days=6)
    await db.commit()


async def test_a_day1_trial_user_is_nudged(db: AsyncSession, starter_user: User, monkeypatch):
    await _make_day1_trial(db, starter_user)
    nudged = await db.run_sync(lambda _s: _run_beat_capturing_nudges(monkeypatch))
    assert starter_user.email in nudged


async def test_an_account_pending_deletion_is_not_nudged(
    db: AsyncSession, starter_user: User, monkeypatch
):
    await _make_day1_trial(db, starter_user)
    # The real lifecycle entry point (migration 112), as the delete route uses it.
    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                     {"u": str(starter_user.id)})
    await db.execute(text("SELECT * FROM request_account_deletion()"))
    await db.commit()
    state, active = (await db.execute(
        text("SELECT deletion_state, is_active FROM users WHERE id = :u"),
        {"u": str(starter_user.id)})).one()
    assert state == "pending" and active is True  # still active: the old filter's gap

    nudged = await db.run_sync(lambda _s: _run_beat_capturing_nudges(monkeypatch))
    assert starter_user.email not in nudged
