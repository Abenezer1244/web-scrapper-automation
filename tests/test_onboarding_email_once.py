"""Each onboarding email goes out at most once, even when the beat runs twice.

Real Postgres and real Redis (no mocks). Two beat containers can briefly coexist
during a deploy and publish the same daily entry twice, and a worker can be handed
a task again; before the claim, both runs sent the day-1 nudge, the day-3 reminder
and the trial-ending warning. pytest's monkeypatch only replaces the senders, to
record which emails the beat decided to send; the senders themselves are covered
in test_email_layout.py.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import User
from src.workers import onboarding_emails
from src.workers.scheduler_helpers import onboarding as beat


@pytest.fixture
def claims(redis_client):
    """The beat's claim keys in the test Redis, removed again afterwards."""
    pattern = beat._claim_key("*", "*") + "*"

    def keys() -> list[str]:
        return sorted(redis_client.scan_iter(match=pattern))

    yield keys
    for key in keys():
        redis_client.delete(key)


def _record_sends(monkeypatch, outcome: str = onboarding_emails.SENT) -> list[tuple]:
    """Replace the three senders with recorders that return ``outcome``."""
    sent: list[tuple] = []
    monkeypatch.setattr(onboarding_emails, "send_day1_nudge",
                        lambda email, days_left: sent.append(("day1", email, days_left)) or outcome)
    monkeypatch.setattr(onboarding_emails, "send_activation_reminder",
                        lambda email, s, h, days_left: sent.append(("day3", email, days_left)) or outcome)
    monkeypatch.setattr(onboarding_emails, "send_trial_ending_email",
                        lambda email, days_left: sent.append(("trial_ending", email, days_left)) or outcome)
    return sent


def _freeze_clock(monkeypatch, at: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(beat, "datetime", _Frozen)


async def _trial(db: AsyncSession, user: User, *, signed_up: datetime, trial_ends: datetime) -> None:
    user.created_at = signed_up
    user.trial_ends_at = trial_ends
    await db.commit()


async def _run(db: AsyncSession) -> None:
    await db.run_sync(lambda _s: beat._send_onboarding_emails_impl())


NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def test_a_second_run_does_not_resend_the_day1_nudge(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))
    sent = _record_sends(monkeypatch)
    await _run(db)
    await _run(db)
    assert sent == [("day1", starter_user.email, 6)]
    assert len(claims()) == 1


async def test_a_second_run_does_not_resend_the_day3_reminder(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=3, hours=1),
                 trial_ends=NOON + timedelta(days=4))
    sent = _record_sends(monkeypatch)
    await _run(db)
    await _run(db)
    assert sent == [("day3", starter_user.email, 4)]


async def test_each_trial_ending_step_sends_once(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    trial_end = NOON + timedelta(days=2, hours=6)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=5), trial_ends=trial_end)
    sent = _record_sends(monkeypatch)
    _freeze_clock(monkeypatch, NOON)            # 2 days left
    await _run(db)
    await _run(db)
    _freeze_clock(monkeypatch, NOON + timedelta(days=1))  # 1 day left, same trial end
    await _run(db)
    await _run(db)
    assert sent == [("trial_ending", starter_user.email, 2), ("trial_ending", starter_user.email, 1)]


async def test_a_duplicate_run_across_utc_midnight_is_still_caught(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    midnight = datetime(2026, 11, 11, tzinfo=UTC)
    # Eligibility is the same on both sides of midnight: 2 days left either way.
    await _trial(db, starter_user, signed_up=midnight - timedelta(days=5),
                 trial_ends=midnight + timedelta(days=2, hours=12))
    sent = _record_sends(monkeypatch)
    _freeze_clock(monkeypatch, midnight - timedelta(seconds=1))
    await _run(db)
    _freeze_clock(monkeypatch, midnight + timedelta(seconds=1))
    await _run(db)
    assert sent == [("trial_ending", starter_user.email, 2)]


async def test_an_ineligible_user_creates_no_claim(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=2),
                 trial_ends=NOON + timedelta(days=5))
    sent = _record_sends(monkeypatch)
    await _run(db)
    assert sent == [] and claims() == []


@pytest.mark.parametrize("outcome", ["not_sent", "skipped", "held"])
async def test_an_email_that_never_reached_resend_is_tried_again(
    db: AsyncSession, starter_user: User, monkeypatch, claims, outcome
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))
    sent = _record_sends(monkeypatch, outcome)
    await _run(db)
    assert claims() == []                       # released
    sent_again = _record_sends(monkeypatch)
    await _run(db)
    assert len(sent) == 1 and sent_again == [("day1", starter_user.email, 6)]


@pytest.mark.parametrize("outcome", [onboarding_emails.FAILED, onboarding_emails.SUPPRESSED,
                                     onboarding_emails.NOT_NEEDED])
async def test_a_resend_error_or_a_final_outcome_keeps_the_claim(
    db: AsyncSession, starter_user: User, monkeypatch, claims, outcome
):
    # FAILED: Resend errored, but may have delivered it, so it is never retried.
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))
    sent = _record_sends(monkeypatch, outcome)
    await _run(db)
    await _run(db)
    assert len(sent) == 1 and len(claims()) == 1


async def test_a_sender_exception_keeps_the_claim(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))

    def boom(email, days_left):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(onboarding_emails, "send_day1_nudge", boom)
    with pytest.raises(RuntimeError):
        await _run(db)
    sent = _record_sends(monkeypatch)
    await _run(db)
    assert sent == [] and len(claims()) == 1


async def test_without_redis_nothing_is_sent(
    db: AsyncSession, starter_user: User, monkeypatch, claims
):
    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))
    sent = _record_sends(monkeypatch)
    # Port 1 on localhost: nothing listens there, so the claim cannot be made.
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(beat, "_REDIS_TIMEOUT_S", 1)
    await _run(db)
    assert sent == []


def test_concurrent_claims_send_once(redis_client, claims):
    key = beat._claim_key("day1", "concurrent-test-user")
    barrier = threading.Barrier(2)
    sent: list[str] = []
    results: list[str] = []
    errors: list[BaseException] = []

    def attempt():
        try:
            client = beat._redis_client()
            try:
                barrier.wait(timeout=5)
                results.append(beat._send_once(
                    client, key, lambda: sent.append("x") or onboarding_emails.SENT))
            finally:
                client.close()
        except BaseException as exc:  # noqa: BLE001 -- reported in the test thread
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert sent == ["x"]
    assert sorted(results) == sorted([onboarding_emails.SENT, beat.DUPLICATE])


def _commercial_ready(monkeypatch) -> None:
    # Test configuration only; the Resend SDK call is replaced in each test.
    monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key-not-used-for-network")
    monkeypatch.setattr(settings, "API_BASE_URL", "https://api.bridgeleads.io")
    monkeypatch.setattr(settings, "COMPANY_POSTAL_ADDRESS",
                        "BridgeLeads test address, 100 Test Ave, Seattle, WA 98101")


def _raise_time_limit(*_args, **_kwargs):
    from celery.exceptions import SoftTimeLimitExceeded

    raise SoftTimeLimitExceeded()


@pytest.mark.parametrize("where", ["opt_out_lookup", "resend_call"])
async def test_a_celery_time_limit_stops_the_run_and_keeps_the_claim(
    db: AsyncSession, starter_user: User, monkeypatch, claims, where
):
    # A time limit must end the task, not count as NOT_SENT (claim released, run
    # continues) or FAILED. Exercised through the real day-1 sender.
    import resend
    from celery.exceptions import SoftTimeLimitExceeded

    _freeze_clock(monkeypatch, NOON)
    await _trial(db, starter_user, signed_up=NOON - timedelta(days=1, hours=1),
                 trial_ends=NOON + timedelta(days=6))
    _commercial_ready(monkeypatch)
    if where == "opt_out_lookup":
        monkeypatch.setattr(onboarding_emails, "_is_suppressed", _raise_time_limit)
    else:
        monkeypatch.setattr(resend.Emails, "send", _raise_time_limit)
    closed: list[bool] = []
    real_client = beat._redis_client

    def tracked_client():
        client = real_client()
        real_close = client.close
        client.close = lambda: closed.append(True) or real_close()
        return client

    monkeypatch.setattr(beat, "_redis_client", tracked_client)
    with pytest.raises(SoftTimeLimitExceeded):
        await _run(db)
    assert len(claims()) == 1 and closed == [True]


def test_releasing_never_removes_another_runs_claim(redis_client, claims):
    key = beat._claim_key("day1", "replaced-claim-user")

    def held_after_replacement():
        # Our claim expired or was lost mid-send and another run claimed it.
        redis_client.set(key, "someone-else")
        return onboarding_emails.HELD

    client = beat._redis_client()
    try:
        assert beat._send_once(client, key, held_after_replacement) == onboarding_emails.HELD
    finally:
        client.close()
    assert redis_client.get(key) == "someone-else"
