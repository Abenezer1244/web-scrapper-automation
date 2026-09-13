"""Skip trace is bought only for the rows that actually ship.

The enqueue used to run at the end of inline enrichment, before the steps that
still decide delivery: the same-run survivor re-election and the plan cap. A
re-election that swapped the survivor left the paid lookup on the row it had
just demoted while the delivered row was never traced, and a capped row could be
traced without ever being delivered. The enqueue now runs after both, and the
dispatcher re-reads the same flags before it claims anything.

Real DB (conftest `db` fixture). Tracerfy is never reached: these tests stop at
the queue.
"""
import uuid

import pytest
from sqlalchemy import func, select

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
from src.config import settings
from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, User
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import (
    collapse_same_run_siblings,
    reconcile_same_run_survivors,
)
from src.workers.tasks_helpers.enrich import (
    _enqueue_skip_trace_rows,
    _run_inline_enrichment,
)

# A traceable owner and a complete, parseable situs: exactly the row the enqueue
# is meant to accept, so a test that sees no pending row is proving a rule and
# not tripping over an ineligible fixture.
_PARTY = "SAARENAS AVELINO G"
_SITUS = {"property_city": "VANCOUVER", "property_state": "WA", "property_zip": "98661"}


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


async def _config(db, user: User) -> ScraperConfig:
    # Clark: no county-specific enrichment branch, so inline enrichment on rows
    # that already carry both addresses and no parcel makes no external call.
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="skip trace ordering",
        county="clark", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []}, skip_trace_enabled=True,
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user: User, cfg: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id,
               status="enriching", trigger="manual"))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, **kw) -> str:
    rid = str(uuid.uuid4())
    kw.setdefault("is_duplicate", False)
    kw.setdefault("party_name", _PARTY)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, **kw))
    await db.commit()
    return rid


def _sync_call(fn, job_id, cfg_id, redis_client):
    """Run a worker helper the way the task does: sync session, ORM job + config."""
    def _inner(s):
        job = s.get(Job, job_id)
        cfg = s.get(ScraperConfig, cfg_id)
        fn(s, job, redis_client, job_id, cfg)
    return _inner


async def _pending_for(db, result_id) -> int:
    return (await db.execute(
        select(func.count()).select_from(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.result_id == result_id)
    )).scalar_one()


async def _status(db, result_id) -> str:
    r = await db.get(Result, result_id)
    await db.refresh(r)
    return r.skip_trace_status


async def test_inline_enrichment_no_longer_buys_lookups(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """Enrichment runs before delivery is decided, so it must not enqueue. The
    second half runs the real enqueue on the same row, which proves the row was
    eligible all along and the first assertion is not vacuous."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id,
                     property_address="1400 MAIN ST",
                     mailing_address="1400 MAIN ST, VANCOUVER, WA 98661", **_SITUS)

    await db.run_sync(_sync_call(_run_inline_enrichment, job_id, cfg.id, redis_client))
    assert await _pending_for(db, rid) == 0
    assert await _status(db, rid) == "not_attempted"

    await db.run_sync(_sync_call(_enqueue_skip_trace_rows, job_id, cfg.id, redis_client))
    assert await _pending_for(db, rid) == 1
    assert await _status(db, rid) == "queued"


async def test_the_lookup_follows_the_re_elected_survivor(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """The defect this ordering fixes. Two addressless filings on one parcel
    collapse; enrichment then gives an address to the row that LOST, the
    re-election swaps them, and only the row that now ships is traced."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    h = legacy_strong_signature("0123456", None)
    first = await _row(db, job_id, business_user.id, dedup_hash=h, parcel_id="0123456",
                       party_name=_PARTY, **_SITUS)
    second = await _row(db, job_id, business_user.id, dedup_hash=h, parcel_id="0123456",
                        party_name="", **_SITUS)

    await db.run_sync(lambda s: collapse_same_run_siblings(s, job_id, business_user.id))
    await db.commit()
    lost = await db.get(Result, second)
    await db.refresh(lost)
    assert lost.is_duplicate is True

    # Enrichment recovers an address for the collapsed row only.
    lost.property_address = "1400 MAIN ST"
    lost.party_name = _PARTY
    await db.commit()
    await db.run_sync(lambda s: reconcile_same_run_survivors(s, job_id, business_user.id))
    await db.commit()

    await db.run_sync(_sync_call(_enqueue_skip_trace_rows, job_id, cfg.id, redis_client))

    assert await _pending_for(db, second) == 1, "the row that ships was not traced"
    assert await _pending_for(db, first) == 0, "a lookup was bought for a demoted row"


async def test_a_row_the_plan_cap_excluded_is_never_traced(
    db, business_user: User, redis_client, _skip_trace_on,
):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    shipped = await _row(db, job_id, business_user.id,
                         property_address="1400 MAIN ST", **_SITUS)
    capped = await _row(db, job_id, business_user.id,
                        property_address="1402 MAIN ST", **_SITUS,
                        enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA})

    await db.run_sync(_sync_call(_enqueue_skip_trace_rows, job_id, cfg.id, redis_client))

    assert await _pending_for(db, shipped) == 1
    assert await _pending_for(db, capped) == 0
    assert await _status(db, capped) == "not_attempted"
