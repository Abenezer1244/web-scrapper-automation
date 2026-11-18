"""Late delivery of rows held for having no address (src/workers/cv_late_delivery.py): real Postgres.

Owner decision 2026-11-15 ("promote and bill"): a row its run held as no_address (plan_cap
mark_no_address_rows) that a sweep later gives a property or mailing address is delivered and
billed once, by the same pipeline that delivers held code violations, behind its own kill
switch. Pinned here: delivered and charged once with a no_address proof; never delivered while
it still has no address, without a claim, privacy-suppressed, with no quota, or with the switch
off; each switch only governs its own hold; both off still finishes already-delivered rows;
skip trace is asked only for a row with a property address; a later run never charges again.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NO_ADDRESS, actionable_condition
from src.config import settings
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig
from src.workers import cv_late_delivery as late
from src.workers.property_identity import legacy_strong_signature
from tests.test_privacy_read_paths import _withhold

MAILING = "PO BOX 210, PORT ANGELES, WA 98362"
SITUS = "1304 S LAUREL ST, PORT ANGELES, WA 98362"


async def _config(db, user, *, record_type="probate", skip=False, deliver=None) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Clallam probate", county="clallam",
        state="WA", record_type=record_type, fields=["party_name", "parcel_id"], enrichment=[],
        schedule={"frequency": "manual"}, deliver=deliver or {"format": "csv", "emails": []},
        skip_trace_enabled=skip)
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, status="done") -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", record_count=0, billed_count=0))
    await db.commit()
    return job_id


async def _held_row(db, user, job, *, parcel=None, claim=True, ed=None, **kw) -> str:
    """A row its run held for having no address, holding its property's claim."""
    parcel = parcel or f"0630{uuid.uuid4().int % 10**8:08d}"
    rid = str(uuid.uuid4())
    h = legacy_strong_signature(parcel, None)
    kw.setdefault("is_duplicate", False)
    db.add(Result(id=rid, job_id=job, user_id=user.id, parcel_id=parcel, dedup_hash=h,
                  party_name="ESTATE OF A TESTER", date_recorded="11/02/2026",
                  enrichment_data={DELIVERY_EXCLUDED_KEY: NO_ADDRESS, **(ed or {})}, **kw))
    await db.commit()
    if claim:
        db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=user.id, dedup_hash=h,
                               first_result_id=rid, first_job_id=job, parcel_id=parcel))
        await db.commit()
    return rid


async def _recover(db, rid, *, mailing=MAILING, situs=None):
    """What the mailing / property sweeps do to a finished run's row."""
    await db.execute(text(
        "UPDATE results SET mailing_address = :m, property_address = :p, property_city = :c, "
        "property_state = 'WA', property_zip = '98362' WHERE id = :i"),
        {"m": mailing, "p": situs, "c": "PORT ANGELES" if situs else None, "i": rid})
    await db.commit()


async def _ed(db, rid) -> dict:
    return (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()


async def _used(db, user) -> int:
    return (await db.execute(text("SELECT records_used FROM users WHERE id = :u"), {"u": user.id})).scalar()


async def _job_numbers(db, job):
    return tuple((await db.execute(text(
        "SELECT billed_count, record_count, breakdown_new FROM jobs WHERE id = :j"), {"j": job})).one())


def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")   # nothing is sent


async def _queued(db, job) -> set[str]:
    return set((await db.execute(text(
        "SELECT result_id::text FROM pending_skip_trace_rows WHERE job_id = :j"), {"j": job})).scalars())


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "NO_ADDRESS_LATE_DELIVERY_ENABLED", True, raising=False)


@pytest.mark.asyncio
async def test_a_recovered_held_row_is_delivered_and_billed_once(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await _recover(db, rid)
    used, numbers = await _used(db, business_user), await _job_numbers(db, job)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1
    ed = await _ed(db, rid)
    assert DELIVERY_EXCLUDED_KEY not in ed
    assert ed[late.LATE_KEY]["policy"] == NO_ADDRESS and ed[late.LATE_KEY]["delivered_at"]
    assert await _used(db, business_user) == used + 1
    assert await _job_numbers(db, job) == numbers                 # the run's numbers never move
    assert (await db.execute(select(func.count()).select_from(Result).where(
        Result.id == rid, actionable_condition()))).scalar() == 1  # a lead now

    assert late.deliver_completed_held_rows()["candidates"] == 0
    assert await _used(db, business_user) == used + 1


@pytest.mark.asyncio
async def test_rows_that_must_not_be_delivered_are_left_alone(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    running = await _job(db, business_user, cfg, status="enriching")
    u = business_user
    still_none = await _held_row(db, u, job)                      # no address found yet
    placeholder = await _held_row(db, u, job)
    no_claim = await _held_row(db, u, job, claim=False)
    sibling = await _held_row(db, u, job, claim=False, is_duplicate=True, duplicate_reason="same_run")
    not_done = await _held_row(db, u, running)
    suppressed = await _held_row(db, u, job)
    await _recover(db, placeholder, mailing="(enrichment unavailable)")
    for rid in (no_claim, sibling, not_done, suppressed):
        await _recover(db, rid)
    await _withhold(db, suppressed)
    used = await _used(db, u)

    assert late.deliver_completed_held_rows()["delivered"] == 0
    for rid in (still_none, placeholder, no_claim, sibling, not_done, suppressed):
        ed = await _ed(db, rid)
        assert ed[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS and late.LATE_KEY not in ed
    assert await _used(db, u) == used


@pytest.mark.asyncio
async def test_no_quota_left_keeps_it_held(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await _recover(db, rid)
    await db.execute(text("UPDATE users SET records_used = records_limit WHERE id = :u"), {"u": business_user.id})
    await db.commit()

    stats = late.deliver_completed_held_rows()

    assert (stats["delivered"], stats["no_quota"]) == (0, 1)
    assert (await _ed(db, rid))[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS


@pytest.mark.asyncio
async def test_each_switch_governs_only_its_own_hold(db, business_user, monkeypatch):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await _recover(db, rid)

    monkeypatch.setattr(settings, "NO_ADDRESS_LATE_DELIVERY_ENABLED", False)
    assert late.deliver_completed_held_rows()["delivered"] == 0
    assert (await _ed(db, rid))[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS

    monkeypatch.setattr(settings, "NO_ADDRESS_LATE_DELIVERY_ENABLED", True)
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", False)
    assert late.deliver_completed_held_rows()["delivered"] == 1


@pytest.mark.asyncio
async def test_a_code_violation_is_never_delivered_by_the_no_address_rule(db, business_user, monkeypatch):
    """Code violations are held as not_qualified and released only by their own policy."""
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", False)
    cfg = await _config(db, business_user, record_type="code_violation")
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await _recover(db, rid, situs=SITUS)

    assert late.deliver_completed_held_rows()["delivered"] == 0
    assert (await _ed(db, rid))[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS


@pytest.mark.asyncio
async def test_both_switches_off_still_finish_rows_already_delivered(db, business_user, monkeypatch):
    _skip_trace_on(monkeypatch)
    cfg = await _config(db, business_user, skip=True)
    job = await _job(db, business_user, cfg)
    stamp = {"delivered_at": "2026-11-15T20:00:00+00:00", "policy": NO_ADDRESS, "notified": False,
             late.CONTACTS_KEY: late.CONTACTS_PENDING}
    done_row = await _held_row(db, business_user, job, ed={late.LATE_KEY: stamp})
    await _recover(db, done_row, situs=SITUS)
    await db.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb - :k)::json "
                          "WHERE id = :i"), {"k": DELIVERY_EXCLUDED_KEY, "i": done_row})
    await db.commit()
    waiting = await _held_row(db, business_user, job)
    await _recover(db, waiting)
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", False)
    monkeypatch.setattr(settings, "NO_ADDRESS_LATE_DELIVERY_ENABLED", False)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 0 and stats["notified_accounts"] == 1
    assert stats["contacts_requested"] == 1 and await _queued(db, job) == {done_row}
    assert (await _ed(db, done_row))[late.LATE_KEY]["notified"] is True
    assert (await _ed(db, waiting))[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS


@pytest.mark.asyncio
async def test_skip_trace_is_asked_only_for_a_row_with_a_property_address(db, business_user):
    cfg = await _config(db, business_user, skip=True)
    job = await _job(db, business_user, cfg)
    mailing_only = await _held_row(db, business_user, job)
    with_situs = await _held_row(db, business_user, job)
    await _recover(db, mailing_only)
    await _recover(db, with_situs, mailing=None, situs=SITUS)

    from src.db.session import system_sync_session
    from src.privacy.authorize import build_snapshot

    with system_sync_session() as s:                   # the delivery step alone, no contact request
        rows = s.execute(text(late._CANDIDATES_SQL),
                         {"batch": 10, "per_account": 10, **late._rule_params()}).mappings().all()
        s.rollback()
        assert sorted(r["id"] for r in rows) == sorted([mailing_only, with_situs])
        assert all(late._deliver_one(s, r, build_snapshot([]), late._rule_params()) == "delivered"
                   for r in rows)

    assert (await _ed(db, mailing_only))[late.LATE_KEY][late.CONTACTS_KEY] == "not_requested"
    assert (await _ed(db, with_situs))[late.LATE_KEY][late.CONTACTS_KEY] == late.CONTACTS_PENDING


@pytest.mark.asyncio
async def test_a_later_run_never_charges_the_property_again(db, business_user):
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.dedup import transfer_undelivered_claims

    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    parcel = "063000038700"
    rid = await _held_row(db, business_user, job, parcel=parcel)
    await _recover(db, rid)
    assert late.deliver_completed_held_rows()["delivered"] == 1

    later = await _job(db, business_user, cfg, status="enriching")
    again = str(uuid.uuid4())
    db.add(Result(id=again, job_id=later, user_id=business_user.id, parcel_id=parcel,
                  mailing_address=MAILING, dedup_hash=legacy_strong_signature(parcel, None),
                  is_duplicate=True, duplicate_reason="prior_run", duplicate_source_job_id=job,
                  enrichment_data={}))
    await db.commit()

    with system_sync_session() as s:
        moved = transfer_undelivered_claims(s, later, business_user.id, "probate")
        s.commit()
    assert moved == 0                                   # the late delivery holds the claim
    assert (await db.execute(text("SELECT first_result_id::text FROM delivered_records "
                                  "WHERE user_id = :u AND dedup_hash = :h"),
                             {"u": business_user.id, "h": legacy_strong_signature(parcel, None)})).scalar() == rid


@pytest.mark.asyncio
async def test_the_contact_lookup_queues_only_the_row_with_a_property_address(db, business_user, monkeypatch):
    """End to end through request_late_contacts: the run's own enqueue, this row only."""
    _skip_trace_on(monkeypatch)
    cfg = await _config(db, business_user, skip=True)
    job = await _job(db, business_user, cfg)
    mailing_only = await _held_row(db, business_user, job)
    with_situs = await _held_row(db, business_user, job)
    await _recover(db, mailing_only)
    await _recover(db, with_situs, situs=SITUS)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 2 and stats["contacts_requested"] == 1
    assert await _queued(db, job) == {with_situs}
    assert (await _ed(db, with_situs))[late.LATE_KEY][late.CONTACTS_KEY] == "requested"
    assert (await _ed(db, mailing_only))[late.LATE_KEY][late.CONTACTS_KEY] == "not_requested"


@pytest.mark.asyncio
async def test_a_promoted_lead_goes_to_the_runs_dialer_once(db, business_user):
    cfg = await _config(db, business_user, deliver={"format": "csv", "emails": [], "dialer_type": "phoneburner"})
    job = await _job(db, business_user, cfg)
    await db.execute(text("UPDATE jobs SET dialer_pushed_at = now() - interval '2 hours' WHERE id = :j"),
                     {"j": job})
    await db.commit()
    rid = await _held_row(db, business_user, job, phone="2065550100")
    await _recover(db, rid, situs=SITUS)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1 and stats["dialer_pushed"] == 1
    outbox = set((await db.execute(text("SELECT result_id::text FROM dialer_deliveries WHERE job_id = :j"),
                                   {"j": job})).scalars())
    assert outbox == {rid} and (await _ed(db, rid))[late.LATE_KEY][late.DIALER_KEY] == "pushed"


@pytest.mark.asyncio
async def test_a_tax_row_past_the_recency_cap_is_never_charged(db, business_user):
    """Codex phase 2 (High): a capped tax row is hidden from every list, export and dialer."""
    from datetime import UTC, datetime

    from src.api.tax_filters import tax_cap_min_year

    cfg = await _config(db, business_user, record_type="tax_delinquent")
    job = await _job(db, business_user, cfg)
    cutoff = tax_cap_min_year(datetime.now(UTC).date())
    capped = await _held_row(db, business_user, job, delinquent_bill_year=cutoff - 1)
    recent = await _held_row(db, business_user, job, delinquent_bill_year=cutoff)
    await _recover(db, capped)
    await _recover(db, recent)
    used = await _used(db, business_user)

    assert late.deliver_completed_held_rows()["delivered"] == 1
    assert (await _ed(db, capped))[DELIVERY_EXCLUDED_KEY] == NO_ADDRESS
    assert late.LATE_KEY in await _ed(db, recent)
    assert await _used(db, business_user) == used + 1
