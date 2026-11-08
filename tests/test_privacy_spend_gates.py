"""Nothing that moves money touches a privacy-withheld row (Phase 6 P3b-1).

The inventory (docs/legal/audit-2026-11/07-RESULTS-READ-INVENTORY.md) found three
paths that bill or buy without the central predicate:
  - late delivery of held code violations (cv_late_delivery._ELIGIBLE): would remove
    the hold and bill one record for a withheld row;
  - the paid skip-trace claim (skip_trace_claim.claim_skip_trace_rows), which every
    paid lookup goes through and which trusted its callers;
  - the operator backfill (scripts/backfill_skip_trace_jobs.py), which now uses the
    production enqueue's eligibility (not asserted here: it is an operator script).

Real Postgres; the flag is set as the enforcer will set it (tests/test_privacy_read_paths._withhold).
Tracerfy is never reached: the claim tests stop at the queue.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NOT_QUALIFIED
from src.db.models import User
from src.privacy.authorize import build_snapshot
from src.workers import cv_late_delivery as late
from tests.test_cv_late_delivery import _config as _cv_config
from tests.test_cv_late_delivery import (
    _enabled,  # noqa: F401 -- autouse kill-switch fixture
    _held_row,
    _state,
    _used,
)
from tests.test_cv_late_delivery import _job as _cv_job
from tests.test_privacy_read_paths import _withhold
from tests.test_skip_trace_claim import _config as _st_config
from tests.test_skip_trace_claim import _job as _st_job
from tests.test_skip_trace_claim import _locked_claim, _payload, _pending, _status
from tests.test_skip_trace_claim import _row as _st_row


@pytest.mark.asyncio
async def test_a_withheld_held_row_is_never_delivered_late_or_billed(db, business_user: User):
    cfg = await _cv_config(db, business_user)
    job = await _cv_job(db, business_user, cfg)
    kept = await _held_row(db, business_user, job)
    withheld = await _held_row(db, business_user, job)
    await _withhold(db, withheld)
    used = await _used(db, business_user)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1  # the unflagged twin, so the tick really ran
    assert DELIVERY_EXCLUDED_KEY not in (await _state(db, kept))[0]
    assert (await _state(db, withheld))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED
    assert await _used(db, business_user) == used + 1


@pytest.mark.asyncio
async def test_a_row_withheld_after_discovery_is_caught_under_the_lock(db, business_user: User):
    from src.db.session import system_sync_session

    cfg = await _cv_config(db, business_user)
    job = await _cv_job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    with system_sync_session() as s:
        [found] = s.execute(text(late._CANDIDATES_SQL),
                            {"batch": 10, "per_account": 10}).mappings().all()
        s.rollback()
    await _withhold(db, rid)
    used = await _used(db, business_user)

    with system_sync_session() as s:
        assert late._deliver_one(s, found, build_snapshot([])) == "stale"
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED
    assert await _used(db, business_user) == used


async def test_the_paid_claim_refuses_a_withheld_row_and_takes_the_rest(
    db, business_user: User,
):
    cfg = await _st_config(db, business_user)
    job_id = await _st_job(db, business_user, cfg)
    kept = await _st_row(db, job_id, business_user.id, address="1 A ST")
    withheld = await _st_row(db, job_id, business_user.id, address="2 B ST")
    payloads = [await _payload(db, kept), await _payload(db, withheld)]
    await _withhold(db, withheld)

    won = await db.run_sync(lambda s: _locked_claim(s, payloads))
    await db.commit()

    assert won == [kept]
    assert await _status(db, kept) == "queued"
    assert await _status(db, withheld) == "not_attempted"
    assert await _pending(db, withheld) == 0


@pytest.mark.asyncio
async def test_a_late_row_withheld_afterwards_is_not_announced_or_traced(
    db, business_user: User, monkeypatch,
):
    """Delivered late while skip trace was switched off (contacts left 'pending'), then
    withheld: the next ticks neither announce it nor buy its contacts, and its markers
    stay pending (so a lifted rule would resume them)."""
    from src.config import settings
    from tests.test_cv_late_contacts import _contacts, _queued, _run, _situs
    from tests.test_cv_late_delivery import _held

    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)  # contacts wait
    notify = late.notify_late_deliveries
    monkeypatch.setattr(late, "notify_late_deliveries", lambda: 0)  # announce later, below
    job = await _run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(**_situs()))
    assert late.deliver_completed_held_rows()["delivered"] == 1
    assert await _contacts(db, rid) == "pending"

    await _withhold(db, rid)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)

    assert late.request_late_contacts() == 0
    assert notify() == 0
    assert await _queued(db, job) == set()
    assert await _contacts(db, rid) == "pending"
    notified = (await db.execute(text(
        "SELECT enrichment_data::jsonb->'late_delivery'->>'notified' FROM results WHERE id = :i"),
        {"i": rid})).scalar()
    assert notified == "false"


@pytest.mark.asyncio
async def test_a_held_row_naming_a_withheld_person_is_flagged_before_any_charge(
    db, business_user: User, monkeypatch,
):
    """P5g: late delivery authorizes the locked row BEFORE grant_records, so a row a
    rule withholds (not yet flagged by the enforcer) is flagged and never charged."""
    import src.privacy.authorize as authorize_mod
    from tests.test_privacy_authorize import _history
    from tests.test_privacy_enforce import _rule_row

    rule = await _rule_row(db)
    rows = _history(["pending", "active"])            # SMITH JOHN
    rows[1]["id"] = rule
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda s: build_snapshot(rows))
    cfg = await _cv_config(db, business_user)
    job = await _cv_job(db, business_user, cfg)
    kept = await _held_row(db, business_user, job)
    named = await _held_row(db, business_user, job)
    await db.execute(text("UPDATE results SET party_name = 'SMITH JOHN' WHERE id = :i"),
                     {"i": named})
    await db.commit()
    used = await _used(db, business_user)
    stats = late.deliver_completed_held_rows()
    assert (stats["delivered"], stats["privacy_withheld"]) == (1, 1)
    flag = (await db.execute(text(
        "SELECT privacy_suppression_id::text FROM results WHERE id = :i"), {"i": named})).scalar()
    assert flag == rule
    assert (await _state(db, named))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED  # still held
    assert DELIVERY_EXCLUDED_KEY not in (await _state(db, kept))[0]
    assert await _used(db, business_user) == used + 1                          # one charge


@pytest.mark.asyncio
async def test_late_delivery_delivers_nothing_when_rules_cannot_be_read(
    db, business_user: User, monkeypatch,
):
    import src.privacy.authorize as authorize_mod
    from src.privacy.authorize import RuleSnapshotError

    def _broken(s):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    cfg = await _cv_config(db, business_user)
    job = await _cv_job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    used = await _used(db, business_user)
    stats = late.deliver_completed_held_rows()
    assert stats["skipped"] == "privacy rules unavailable" and stats["delivered"] == 0
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED
    assert await _used(db, business_user) == used
