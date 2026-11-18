"""scripts/backfill_hold_unbilled_no_address.py on real Postgres: it stamps exactly the past
unbilled no-address leads, refuses a count that does not match, and the stamped rows are then
billed once by the late-delivery tick."""
from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NO_ADDRESS
from src.config import settings
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig
from src.workers import cv_late_delivery as late

_spec = importlib.util.spec_from_file_location(
    "backfill_hold_unbilled_no_address",
    Path(__file__).parent.parent / "scripts" / "backfill_hold_unbilled_no_address.py")
bf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bf
_spec.loader.exec_module(bf)

BILLED = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
AFTER = (BILLED + timedelta(hours=3)).isoformat()
BEFORE = (BILLED - timedelta(hours=3)).isoformat()
MAILING = "PO BOX 9, VANCOUVER, WA 98660"


async def _run(db, user, record_type="probate") -> str:
    cfg = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="Clark probate", county="clark",
                        state="WA", record_type=record_type, fields=["party_name"], enrichment=[],
                        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(cfg)
    await db.commit()
    job = str(uuid.uuid4())
    db.add(Job(id=job, user_id=user.id, scraper_config_id=cfg.id, status="done", trigger="manual",
               record_count=1, billed_count=1, billing_applied_at=BILLED))
    await db.commit()
    return job


async def _lead(db, user, job, *, prop=None, mailing=MAILING, ed=None, claim=True, **kw) -> str:
    rid, h = str(uuid.uuid4()), uuid.uuid4().hex
    kw.setdefault("is_duplicate", False)
    db.add(Result(id=rid, job_id=job, user_id=user.id, parcel_id="986000000001", dedup_hash=h,
                  party_name="ESTATE OF A TESTER", property_address=prop, mailing_address=mailing,
                  enrichment_data=ed if ed is not None else
                  {"mailing_recovery_outcome": "found", "mailing_recovery_last_at": AFTER}, **kw))
    await db.commit()
    if claim:
        db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=user.id, dedup_hash=h,
                               first_result_id=rid, first_job_id=job))
        await db.commit()
    return rid


async def _reason(db, rid):
    return (await db.execute(text("SELECT enrichment_data->>:k FROM results WHERE id = :i"),
                             {"k": DELIVERY_EXCLUDED_KEY, "i": rid})).scalar()


def _go(user, runs, expect, apply):
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        return bf.run(s, user_id=str(user.id), runs=runs, expect=expect, apply=apply)


@pytest.mark.asyncio
async def test_only_leads_with_no_address_at_billing_are_stamped_then_billed_once(db, business_user, monkeypatch):
    monkeypatch.setattr(settings, "NO_ADDRESS_LATE_DELIVERY_ENABLED", True, raising=False)
    u = business_user
    job = await _run(db, u)
    cv_job = await _run(db, u, record_type="code_violation")
    target = await _lead(db, u, job)                                       # mailing found after billing
    situs_after = await _lead(db, u, job, prop="1 MAIN ST", mailing=None,
                              ed={"property_recovery_last_at": AFTER})       # property found after billing
    keep = {
        "had_mailing_at_billing": await _lead(db, u, job, ed={"mailing_recovery_outcome": "found",
                                                              "mailing_recovery_last_at": BEFORE}),
        "had_property": await _lead(db, u, job, prop="2 MAIN ST", mailing=None, ed={}),
        "no_claim": await _lead(db, u, job, claim=False),
        "duplicate": await _lead(db, u, job, is_duplicate=True, duplicate_reason="same_run"),
        "still_no_address": await _lead(db, u, job, mailing=None),
        "code_violation": await _lead(db, u, cv_job),
    }

    dry = _go(u, [job, cv_job], 2, apply=False)
    assert dry["found"] == 2 and dry["stamped"] == 0 and await _reason(db, target) is None

    assert _go(u, [job, cv_job], 3, apply=True)["refused"]                 # wrong count: nothing written
    assert await _reason(db, target) is None

    assert _go(u, [job, cv_job], 2, apply=True)["stamped"] == 2
    assert [await _reason(db, r) for r in (target, situs_after)] == [NO_ADDRESS, NO_ADDRESS]
    for name, rid in keep.items():
        assert await _reason(db, rid) is None, name

    used = (await db.execute(text("SELECT records_used FROM users WHERE id = :u"), {"u": u.id})).scalar()
    assert late.deliver_completed_held_rows()["delivered"] == 2
    assert (await db.execute(text("SELECT records_used FROM users WHERE id = :u"), {"u": u.id})).scalar() == used + 2
    for rid in (target, situs_after):
        ed = (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()
        assert ed[late.LATE_KEY]["policy"] == NO_ADDRESS and DELIVERY_EXCLUDED_KEY not in ed
    assert late.deliver_completed_held_rows()["delivered"] == 0             # a second tick never charges
    assert (await db.execute(text("SELECT records_used FROM users WHERE id = :u"), {"u": u.id})).scalar() == used + 2
    assert _go(u, [job, cv_job], 0, apply=True)["found"] == 0             # a re-run finds nothing
