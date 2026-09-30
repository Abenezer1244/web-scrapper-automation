"""Audit #4 4b-ii: the customer is told when contact lookups were held back.

#374 made the claim hold every lead an account may not buy: a trial past its
lifetime allowance, a frozen account, an ended plan. The held leads stayed
'not_attempted' and the customer's job log said nothing, while the ops log
counted them as unexplained "not claimed" leads. The enqueue now publishes ONE
line saying how many leads were held and why, after its commit, and the ops
count leaves held leads out.

Real DB, the real ``_enqueue_skip_trace_rows`` (the step run_scrape_job runs),
the real claim. Tracerfy is never reached: the tests stop at the queue.
"""
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.config import settings
from src.db.models import JobLog
from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows
from tests.test_audit4_paid_skip_trace_gate import _account
from tests.test_skip_trace_enqueue_after_delivery import (
    _PARTY,
    _SITUS,
    _config,
    _job,
    _pending_for,
    _row,
    _status,
    _sync_call,
)


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


@pytest.fixture
def allowance(monkeypatch):
    def _set(n: int):
        monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", n)
    _set(3)
    return _set


_TRIAL = {"trial_ends_at": datetime.now(UTC) + timedelta(days=5)}
_FROZEN = {"subscription_status": "unpaid"}
_ENDED = {"subscription_status": "active",
          "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)}


async def _leads(db, user, n: int):
    """A job with ``n`` eligible, distinct leads; returns (config, job_id, result ids)."""
    cfg = await _config(db, user)
    job_id = await _job(db, user, cfg)
    rids = []
    for i in range(n):
        rids.append(await _row(
            db, job_id, user.id, party_name=f"{_PARTY} {i}",
            property_address=f"{1400 + i} MAIN ST",
            mailing_address=f"{1400 + i} MAIN ST, VANCOUVER, WA 98661", **_SITUS,
        ))
    return cfg, job_id, rids


async def _enqueue(db, cfg, job_id, redis_client) -> list[str]:
    """Run the enqueue; return the lines it wrote to the customer's job log."""
    await db.run_sync(_sync_call(_enqueue_skip_trace_rows, job_id, cfg.id, redis_client))
    await db.commit()
    return list((await db.execute(
        select(JobLog.message).where(JobLog.job_id == job_id).order_by(JobLog.created_at)
    )).scalars())


def _held_lines(lines):
    return [m for m in lines if m.startswith("Contact lookups were not run for")]


async def test_a_trial_past_its_allowance_is_told_how_many_leads_were_held(
    db, redis_client, _skip_trace_on, allowance,
):
    """REGRESSION: 5 leads, room for 2. Before 4b-ii the log said nothing."""
    allowance(2)
    trial = await _account(db, **_TRIAL)
    cfg, job_id, rids = await _leads(db, trial, 5)

    lines = await _enqueue(db, cfg, job_id, redis_client)

    assert _held_lines(lines) == [
        "Contact lookups were not run for 3 lead(s): your free trial includes up to 2 "
        "lookup credits, and not enough remain for these leads. Paid plans include "
        "contact lookups for new leads."
    ]
    queued = [rid for rid in rids if await _pending_for(db, rid)]
    assert len(queued) == 2
    for rid in set(rids) - set(queued):
        assert await _pending_for(db, rid) == 0
        assert await _status(db, rid) == "not_attempted"


async def test_a_trial_with_no_allowance_is_told_the_trial_has_none(
    db, redis_client, _skip_trace_on, allowance,
):
    allowance(0)
    trial = await _account(db, **_TRIAL)
    cfg, job_id, _ = await _leads(db, trial, 2)

    assert _held_lines(await _enqueue(db, cfg, job_id, redis_client)) == [
        "Contact lookups were not run for 2 lead(s): your free trial does not include "
        "contact lookups. Paid plans include contact lookups for new leads."
    ]


@pytest.mark.parametrize(("kw", "reason"), [
    (_FROZEN, "your account is frozen because a payment did not go through."),
    (_ENDED, "your paid plan has ended."),
])
async def test_a_blocked_account_is_told_why(db, redis_client, _skip_trace_on, allowance, kw, reason):
    user = await _account(db, **kw)
    cfg, job_id, rids = await _leads(db, user, 2)

    lines = await _enqueue(db, cfg, job_id, redis_client)

    assert _held_lines(lines) == [f"Contact lookups were not run for 2 lead(s): {reason}"]
    for rid in rids:
        assert await _pending_for(db, rid) == 0


async def test_a_paying_account_gets_no_held_line(db, redis_client, _skip_trace_on, allowance):
    """CONTROL: the same leads on an active subscription are all queued, and the
    customer is told nothing about holds."""
    user = await _account(db, subscription_status="active")
    cfg, job_id, rids = await _leads(db, user, 3)

    assert _held_lines(await _enqueue(db, cfg, job_id, redis_client)) == []
    for rid in rids:
        assert await _pending_for(db, rid) == 1


async def test_held_leads_are_not_counted_as_unexplained_losses(
    db, redis_client, _skip_trace_on, allowance,
):
    """The ops line "N lead(s) were not claimed" is for leads nobody can explain.
    Leads held on purpose were counted in it; now they are not."""
    user = await _account(db, **_FROZEN)
    cfg, job_id, _ = await _leads(db, user, 2)
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Capture(logging.INFO)
    logger = logging.getLogger("worker.task")
    logger.addHandler(handler)
    try:
        await _enqueue(db, cfg, job_id, redis_client)
    finally:
        logger.removeHandler(handler)

    assert not [m for m in seen if "were not claimed" in m]


@pytest.mark.parametrize("access", ["trial", "starter", "frozen", "ended"])
def test_no_line_uses_an_em_dash_or_promises_a_later_lookup(access):
    """House style bans em dashes, and nothing re-queues a held lead (enqueue is
    job-scoped), so no line may say it will be looked up later."""
    from src.workers.skip_trace_claim import held_lookup_message

    for allowance_n in (0, 25):
        line = held_lookup_message(access, 4, allowance_n)
        assert line and "—" not in line
        assert not any(w in line.lower() for w in ("later", "will be", "once you", "automatically"))


def test_no_line_when_nothing_was_held_or_access_is_full():
    from src.workers.skip_trace_claim import held_lookup_message

    assert held_lookup_message("trial", 0, 25) is None
    assert held_lookup_message("full", 3, 25) is None


def test_the_held_line_is_published_after_the_enqueue_commit():
    """_publish_log commits, and the job's claim lock must stay held until the
    enqueue's own commit (see the fence in tests/test_finalize_fence.py). So the
    held line must come after that commit, never inside the locked region."""
    import inspect

    src = inspect.getsource(_enqueue_skip_trace_rows)
    claim = src.index("claim_skip_trace_rows(db, to_claim")
    commit = src.index("db.commit()", claim)
    publish = src.index('_publish_log(r, job_id, "info", _held_line')
    assert claim < commit < publish
