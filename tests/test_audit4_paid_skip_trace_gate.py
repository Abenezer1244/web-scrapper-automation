"""Paid contact lookups only for accounts that may buy them (audit #3 S3-03, audit #4 S4-01).

Every Tracerfy lookup is charged to the operator and recovered only from a live paid
subscription. Before this fix the only gate was "the plan is not Starter", and a free
trial is `plan='pro'`, so a trial bought lookups like a paying Pro account (owner
decision 2026-09-27: a small lifetime allowance). And nothing re-checked an account
that froze or ended after its job was queued: the worker ran it, and the dispatcher
sent rows it had queued earlier.

Real DB, no network. The claim tests stop at the queue. The dispatcher tests point
Tracerfy at a non-HTTPS URL, so a row the pass CLAIMS ends 'errored' before any socket
opens, and a row it leaves alone stays 'queued' (the harness of
test_skip_trace_credit_cap.py). The worker tests use a county with no connector, so an
unguarded run stops at connector lookup instead of scraping anything.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.api.quota import ENDED_MESSAGE, FROZEN_MESSAGE
from src.config import settings
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal, system_sync_session
from src.workers.skip_trace_claim import (
    ACCESS_ENDED,
    ACCESS_FROZEN,
    ACCESS_FULL,
    ACCESS_STARTER,
    ACCESS_TRIAL,
    paid_lookup_access,
)
from src.workers.skip_trace_dispatcher import dispatch_pending_skip_trace
from tests.test_skip_trace_claim import _config, _job, _locked_claim, _payload, _row
from tests.test_skip_trace_credit_cap import _claimed, _seed

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class _U:
    """The columns paid_lookup_access reads, nothing else."""

    def __init__(self, **kw):
        self.plan = kw.get("plan", "pro")
        self.is_admin = kw.get("is_admin", False)
        self.subscription_status = kw.get("subscription_status")
        self.trial_ends_at = kw.get("trial_ends_at")
        self.entitlement_ends_at = kw.get("entitlement_ends_at")
        self.entitlement_grace_ends_at = kw.get("entitlement_grace_ends_at")


_TRIAL_END = _NOW + timedelta(days=3)


@pytest.mark.parametrize(("user", "expected"), [
    # The app trial: Pro plan, trial date, no subscription. THE bug.
    (_U(trial_ends_at=_TRIAL_END), ACCESS_TRIAL),
    # Checkout opened but never paid leaves the same state (stripe_customer_id is not
    # read at all: it is written before payment).
    (_U(trial_ends_at=_NOW - timedelta(days=1)), ACCESS_TRIAL),
    (_U(subscription_status="trialing"), ACCESS_TRIAL),
    (_U(subscription_status="canceled"), ACCESS_TRIAL),
    (_U(subscription_status="some_future_status"), ACCESS_TRIAL),
    (_U(subscription_status="past_due"), ACCESS_TRIAL),  # no grace recorded: not proven
    (_U(subscription_status="active"), ACCESS_FULL),
    (_U(subscription_status="past_due",
        entitlement_grace_ends_at=_NOW + timedelta(days=2)), ACCESS_FULL),
    (_U(plan="business"), ACCESS_FULL),  # operator-granted: no status, no trial date
    (_U(is_admin=True, trial_ends_at=_TRIAL_END), ACCESS_FULL),
    (_U(plan="starter", subscription_status="active"), ACCESS_STARTER),
    (_U(plan="starter", is_admin=True), ACCESS_STARTER),
    (_U(subscription_status="unpaid"), ACCESS_FROZEN),
    (_U(subscription_status="past_due",
        entitlement_grace_ends_at=_NOW - timedelta(minutes=1)), ACCESS_FROZEN),
    (_U(subscription_status="active", is_admin=True,
        entitlement_ends_at=_NOW - timedelta(seconds=1)), ACCESS_ENDED),
    (_U(subscription_status="active", entitlement_ends_at=_NOW + timedelta(days=1)),
     ACCESS_FULL),
    # Cancelled at period end, paid through it: full until the term ends (Codex 4a).
    (_U(subscription_status="canceled", entitlement_ends_at=_NOW + timedelta(days=1)),
     ACCESS_FULL),
])
def test_the_access_matrix(user, expected):
    assert paid_lookup_access(user, _NOW) == expected


# ── The claim: where every lookup enters the queue ────────────────────────────


@pytest.fixture
def allowance(monkeypatch):
    def _set(n: int):
        monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", n)
    _set(3)
    return _set


async def _account(db, **kw) -> User:
    user = User(
        id=str(uuid.uuid4()), email=f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"), plan=kw.pop("plan", "pro"),
        records_used=0, records_limit=5000, **kw,
    )
    db.add(user)
    await db.commit()
    return user


async def _payloads(db, user: User, n: int, *, trace_type: str | None = None):
    cfg = await _config(db, user)
    job_id = await _job(db, user, cfg)
    out = []
    for i in range(n):
        p = await _payload(db, await _row(db, job_id, user.id, address=f"{i + 1} TRIAL ST"))
        if trace_type:
            p["trace_type"] = trace_type
        out.append(p)
    return out


async def _claim(db, payloads, report=None):
    won = await db.run_sync(lambda s: _locked_claim(s, payloads, report=report))
    await db.commit()
    return won


async def _queued_rows(db, user_id) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM pending_skip_trace_rows WHERE user_id = CAST(:u AS uuid)"
    ), {"u": user_id})).scalar_one()


async def test_a_trial_claims_up_to_its_allowance_and_holds_the_rest(db, allowance):
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    report: dict = {}
    won = await _claim(db, await _payloads(db, trial, 5), report)
    assert len(won) == 3
    assert report == {"access": ACCESS_TRIAL, "held": 2}
    assert await _queued_rows(db, trial.id) == 3


async def test_the_allowance_is_lifetime_across_jobs(db, allowance):
    """The second job gets only the remainder; a later job gets nothing."""
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    assert len(await _claim(db, await _payloads(db, trial, 2))) == 2
    assert len(await _claim(db, await _payloads(db, trial, 4))) == 1
    assert await _claim(db, await _payloads(db, trial, 2)) == []
    assert await _queued_rows(db, trial.id) == 3


async def test_a_cancelled_or_errored_row_still_counts(db, allowance):
    """Releasing a row does not hand its allowance back."""
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    assert len(await _claim(db, await _payloads(db, trial, 3))) == 3
    await db.execute(text(
        "UPDATE pending_skip_trace_rows SET status = 'cancelled' "
        "WHERE user_id = CAST(:u AS uuid)"), {"u": trial.id})
    await db.commit()
    assert await _claim(db, await _payloads(db, trial, 2)) == []


async def test_two_concurrent_claims_of_one_trial_cannot_both_spend_it(db, allowance):
    """The user-row lock serializes claimers: the second waits for the first's
    commit, then reads the spend it committed. Without the lock both read 3 free
    credits and 6 lookups are bought."""
    import threading
    import time

    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    first, second = await _payloads(db, trial, 3), await _payloads(db, trial, 3)
    result: dict = {}

    def _second():
        with SyncSessionLocal() as s2:
            result["won"] = _locked_claim(s2, second)
            s2.commit()

    with SyncSessionLocal() as s1:
        assert len(_locked_claim(s1, first)) == 3  # holds the user lock, uncommitted
        t = threading.Thread(target=_second)
        t.start()
        time.sleep(1.5)
        assert t.is_alive(), "the second claim did not wait for the first"
        s1.commit()
    t.join(timeout=30)
    assert not t.is_alive()
    assert result["won"] == []
    assert await _queued_rows(db, trial.id) == 3


async def test_an_advanced_lookup_costs_two_credits(db, allowance):
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    won = await _claim(db, await _payloads(db, trial, 3, trace_type="advanced"))
    assert len(won) == 1  # 2 of 3 credits; the next advanced one does not fit
    assert len(await _claim(db, await _payloads(db, trial, 2, trace_type="normal"))) == 1


async def test_allowance_zero_means_no_trial_lookups(db, allowance):
    allowance(0)
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    assert await _claim(db, await _payloads(db, trial, 2)) == []


@pytest.mark.parametrize("kw", [
    {"subscription_status": "active"},
    {"plan": "business"},  # operator-granted
    {"is_admin": True, "trial_ends_at": datetime.now(UTC) + timedelta(days=5)},
    {"subscription_status": "past_due",
     "entitlement_grace_ends_at": datetime.now(UTC) + timedelta(days=2)},
])
async def test_paying_accounts_are_not_limited_by_the_trial_allowance(db, allowance, kw):
    user = await _account(db, **kw)
    report: dict = {}
    assert len(await _claim(db, await _payloads(db, user, 5), report)) == 5
    assert report == {"access": ACCESS_FULL, "held": 0}


@pytest.mark.parametrize(("kw", "access"), [
    ({"plan": "starter"}, ACCESS_STARTER),
    ({"subscription_status": "unpaid"}, ACCESS_FROZEN),
    ({"subscription_status": "active",
      "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)}, ACCESS_ENDED),
])
async def test_blocked_accounts_claim_nothing(db, allowance, kw, access):
    user = await _account(db, **kw)
    report: dict = {}
    assert await _claim(db, await _payloads(db, user, 2), report) == []
    assert report == {"access": access, "held": 2}
    assert await _queued_rows(db, user.id) == 0


# ── The dispatcher: rows queued before the account froze or ended ─────────────


@pytest.fixture
def dispatcher(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.invalid")
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    for name in ("SKIP_TRACE_DAILY_CREDIT_CAP", "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP",
                 "SKIP_TRACE_DAILY_ROW_CAP"):
        monkeypatch.setattr(settings, name, None)


def _states(pending_ids) -> set[tuple[str, str]]:
    """(queue row status, lead skip_trace_status) for these queue rows."""
    with system_sync_session() as s:
        return {(p, r) for p, r in s.execute(text(
            "SELECT p.status, r.skip_trace_status FROM pending_skip_trace_rows p "
            "JOIN results r ON r.id = p.result_id "
            "WHERE p.id = ANY(CAST(:ids AS uuid[]))"), {"ids": list(pending_ids)}).all()}


@pytest.mark.parametrize(("kw", "expected"), [
    # Frozen is usually temporary (dunning): held, untouched, sent once it clears.
    ({"subscription_status": "unpaid"}, {("queued", "queued")}),
    # Ended and Starter are not: withdrawn, lead back to not_attempted, never charged.
    ({"subscription_status": "active",
      "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)},
     {("cancelled", "not_attempted")}),
    ({"plan": "starter"},  # e.g. a trial that expired with lookups still queued
     {("cancelled", "not_attempted")}),
])
async def test_rows_of_a_blocked_account_are_never_sent_while_others_are(
        db, dispatcher, kw, expected):
    blocked = await _account(db, **kw)
    paying = await _account(db, subscription_status="active")
    held = [_seed(blocked.id) for _ in range(2)]
    sent = [_seed(paying.id) for _ in range(2)]

    dispatch_pending_skip_trace()

    assert _claimed(held) == set()
    assert _claimed(sent) == set(sent)
    assert _states(held) == expected


async def test_an_account_being_written_right_now_is_skipped_not_waited_on(db, dispatcher):
    """A billing write holding the users row: the dispatcher neither waits (every
    tenant's pass would stall behind it) nor sends that account's rows on a stale
    read. They stay queued for the next tick; everyone else is served."""
    busy = await _account(db, subscription_status="active")
    other = await _account(db, subscription_status="active")
    waiting = [_seed(busy.id) for _ in range(2)]
    sent = [_seed(other.id) for _ in range(2)]

    with SyncSessionLocal() as writer:
        writer.execute(text("SELECT 1 FROM users WHERE id = CAST(:u AS uuid) FOR UPDATE"),
                       {"u": busy.id})
        dispatch_pending_skip_trace()
        writer.rollback()

    assert _claimed(waiting) == set()
    assert _claimed(sent) == set(sent)


# ── The worker: a job queued before its account froze or ended ────────────────

_NO_CONNECTOR_COUNTY = "audit4-no-connector"


def _queued_job(**user_kw) -> str:
    with SyncSessionLocal() as s:
        user = User(
            id=str(uuid.uuid4()), email=f"audit4_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"), plan=user_kw.pop("plan", "pro"),
            records_used=0, records_limit=500, **user_kw,
        )
        s.add(user)
        s.flush()
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user.id, name="audit4 guard",
            county=_NO_CONNECTOR_COUNTY, state="WA", record_type="probate",
            fields=[], enrichment=[], schedule={}, deliver={}, active=True,
        )
        s.add(cfg)
        s.flush()
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                  status="pending", trigger="manual")
        s.add(job)
        s.commit()
        return job.id


def _run(job_id: str) -> Job:
    from src.workers.tasks import run_scrape_job

    run_scrape_job(job_id)
    with SyncSessionLocal() as s:
        return s.query(Job).filter(Job.id == job_id).one()


@pytest.mark.parametrize(("user_kw", "message"), [
    ({"subscription_status": "unpaid"}, FROZEN_MESSAGE),
    ({"subscription_status": "active",
      "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)}, ENDED_MESSAGE),
])
def test_the_worker_refuses_a_job_whose_account_can_no_longer_run(db, user_kw, message):
    job = _run(_queued_job(**user_kw))
    assert job.status == "failed"
    assert job.error_message == message
    assert job.record_count == 0


@pytest.mark.parametrize(("user_kw", "expected"), [
    ({"subscription_status": "unpaid"}, "frozen"),
    ({"subscription_status": "active",
      "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)}, "ended"),
    ({"subscription_status": "active"}, None),
    ({}, None),  # operator-granted
    ({"trial_ends_at": datetime.now(UTC) + timedelta(days=3)}, None),  # trial: records ok
])
def test_account_charge_block_decides_from_the_locked_row(db, user_kw, expected):
    from src.workers.tasks import account_charge_block

    job_id = _queued_job(**user_kw)
    with SyncSessionLocal() as s:
        uid = s.get(Job, job_id).user_id
        assert account_charge_block(s, uid, lock="FOR UPDATE") == expected
        s.rollback()


def test_account_charge_block_reads_the_clock_after_the_lock(db):
    """The decision clock is read once the users row lock is HELD: a billing write
    that ends the term while the reservation waits must not be judged at the
    moment the wait began (Codex 4a review round 2)."""
    import threading
    import time

    from src.workers.tasks import account_charge_block

    job_id = _queued_job(subscription_status="active")
    with SyncSessionLocal() as s:
        uid = s.get(Job, job_id).user_id
    result: dict = {}

    def _reserve():
        with SyncSessionLocal() as s2:
            result["block"] = account_charge_block(s2, uid, lock="FOR UPDATE")
            s2.rollback()

    with SyncSessionLocal() as writer:
        writer.execute(text(
            "UPDATE users SET entitlement_ends_at = clock_timestamp() + interval '4 seconds' "
            "WHERE id = CAST(:u AS uuid)"), {"u": uid})
        t = threading.Thread(target=_reserve)
        t.start()
        time.sleep(6)  # the term ends while the reservation waits on the lock
        writer.commit()
    t.join(timeout=30)
    assert result["block"] == "ended"


def test_an_eligible_account_passes_the_preflight(db):
    """Control: the guard is not what stops this run; the missing connector is."""
    job = _run(_queued_job(subscription_status="active"))
    assert job.status == "failed"
    assert job.error_message not in (FROZEN_MESSAGE, ENDED_MESSAGE)
