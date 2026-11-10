"""The skip-trace dispatcher's admission gate: one admitted execution per tick.

SKIP_TRACE_MAX_BATCHES_PER_TICK is per execution, and two executions can land
seconds apart (two beat containers during a deploy, a redelivered task). The gate
admits the first and drops the second. Real Postgres and real Redis; only the
Tracerfy HTTP seam (`submit_batch`) is replaced, as in test_skip_trace_spend_ledger,
so nothing leaves the process and no credits are spent.

The claim lock and the spend caps are covered in test_skip_trace_credit_cap; the
gate never replaces them.
"""

import logging
import time
import uuid

import pytest
import redis as sync_redis
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

from src.config import settings
from src.db.session import system_sync_session
from src.scrapers.enrichment import skip_trace
from src.utils import skip_trace_pause_state as pause
from src.workers import skip_trace_dispatcher as dispatcher
from tests.test_skip_trace_spend_ledger import _queue_id, _row, _seed


def _seed_one(user_id) -> dict:
    """One queued lead at an address no other row has: the dispatcher holds back a
    second lookup for an address already in flight, which would otherwise keep a
    row queued for a reason that has nothing to do with the gate."""
    (row,) = _seed(user_id)
    addr = f"{uuid.uuid4().hex} GATE TEST AVE"
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET property_address = :a WHERE id = :r"),
                   {"a": addr, "r": row["result"]})
        db.execute(text("UPDATE pending_skip_trace_rows SET property_address = :a WHERE id = :p"),
                   {"a": addr, "p": row["pending"]})
        db.commit()
    return row


@pytest.fixture
def r():
    client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    client.delete(dispatcher._ADMISSION_GATE_KEY)
    yield client
    client.delete(dispatcher._ADMISSION_GATE_KEY)
    client.close()


@pytest.fixture
def posts(monkeypatch):
    """Dispatcher on, alerts off; every Tracerfy POST is recorded and accepted."""
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    calls: list[list[dict]] = []

    def _submit(rows, trace_type="normal", api_token=None):
        calls.append(rows)
        return {"queue_id": _queue_id(), "rows_uploaded": len(rows), "credits_deducted": 0}

    monkeypatch.setattr(skip_trace, "submit_batch", _submit)
    return calls


async def test_a_duplicate_in_the_same_tick_submits_nothing(business_user, posts, r):
    _seed_one(business_user.id)
    out = dispatcher.dispatch_pending_skip_trace()
    assert out["submitted_batches"] == 1 and len(posts) == 1
    gate_ms = r.pttl(dispatcher._ADMISSION_GATE_KEY)
    assert dispatcher._admission_gate_seconds() * 1000 - 5000 < gate_ms <= 225_000
    published = r.hget(pause.KEY, "published_at")
    assert published is not None

    second = _seed_one(business_user.id)  # real work waiting, so the skip is not trivial
    assert dispatcher.dispatch_pending_skip_trace() == {"skipped": "duplicate"}

    assert len(posts) == 1                                   # no second POST
    assert _row(second["pending"]).status == "queued"        # left for the next tick
    assert r.pttl(dispatcher._ADMISSION_GATE_KEY) <= gate_ms  # the duplicate did not extend it
    assert r.hget(pause.KEY, "published_at") == published     # nor publish


async def test_the_next_tick_is_admitted_once_the_gate_expires(business_user, posts, r):
    _seed_one(business_user.id)
    dispatcher.dispatch_pending_skip_trace()
    second = _seed_one(business_user.id)

    r.pexpire(dispatcher._ADMISSION_GATE_KEY, 50)
    deadline = time.monotonic() + 2
    while r.exists(dispatcher._ADMISSION_GATE_KEY) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not r.exists(dispatcher._ADMISSION_GATE_KEY)

    out = dispatcher.dispatch_pending_skip_trace()
    assert out["submitted_batches"] == 1 and len(posts) == 2
    assert _row(second["pending"]).status == "submitted"


async def test_without_redis_the_tick_still_runs(business_user, posts, monkeypatch, caplog):
    """Fails OPEN: the gate is spacing, not safety (the caps live in Postgres), and
    failing closed would stall every customer's lookups during a Redis blip."""
    row = _seed_one(business_user.id)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")

    with caplog.at_level(logging.WARNING):
        out = dispatcher.dispatch_pending_skip_trace()

    assert out["submitted_batches"] == 1 and len(posts) == 1
    assert _row(row["pending"]).status == "submitted"
    assert any("admission gate unavailable" in rec.getMessage() for rec in caplog.records)


async def test_a_tick_that_raises_keeps_the_gate_and_still_publishes(posts, r, monkeypatch):
    def broken_tick():
        raise RuntimeError("tick failed")

    monkeypatch.setattr(dispatcher, "_dispatch_tick", broken_tick)
    with pytest.raises(RuntimeError):
        dispatcher.dispatch_pending_skip_trace()

    assert r.pttl(dispatcher._ADMISSION_GATE_KEY) > 0
    assert r.hget(pause.KEY, "published_at") is not None


@pytest.mark.parametrize("switch", ["SKIP_TRACE_ENABLED", "TRACERFY_API_TOKEN"])
async def test_a_switched_off_dispatcher_still_tombstones_inside_the_gate(
    posts, r, monkeypatch, switch
):
    r.set(dispatcher._ADMISSION_GATE_KEY, "1", ex=200)
    monkeypatch.setattr(settings, switch, False if switch == "SKIP_TRACE_ENABLED" else "")

    out = dispatcher.dispatch_pending_skip_trace()

    assert out["skipped"] in ("disabled", "no_token")
    assert r.hget(pause.KEY, "state") == "disabled"


async def test_a_celery_time_limit_is_not_read_as_redis_down(posts, r, monkeypatch):
    """Fail-open must not swallow a time limit: the task has to stop."""
    def limit():
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(dispatcher, "_admission_gate_seconds", limit)
    with pytest.raises(SoftTimeLimitExceeded):
        dispatcher.dispatch_pending_skip_trace()
    assert posts == []
