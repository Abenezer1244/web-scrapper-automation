"""No-address rows are held when their run delivers: real Postgres, real statements.

The mailing sweep fills a mailing address on rows of finished runs, and a lead needs a
property OR mailing address, so a row the run counted "no address" and never billed
became a live new lead hours later (Clark probate 62404bd0 billed 643 and listed 655,
2026-11-15). These tests pin the hold that freezes the decision with the run, for every
record type but code violation (held as not_qualified already): the recovered address
does not make the row a lead, a bill, a cap slot or an export row; it stays in the No
address list and bucket; a later run can still take its claim and deliver it.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import and_, func, select, text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NO_ADDRESS,
    actionable_condition,
    shown_condition,
)
from src.api.results_category import category_condition, list_condition
from src.api.run_breakdown import read_partition
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig, User
from src.db.session import system_sync_session
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import NO_ADDRESS_NOT_BILLED_SINCE, transfer_undelivered_claims
from src.workers.tasks_helpers.plan_cap import mark_no_address_rows, mark_over_quota_rows

AFTER_RULE = NO_ADDRESS_NOT_BILLED_SINCE + timedelta(days=2)
RECOVERED_MAILING = "PO BOX 210, PORT ANGELES, WA 98362"


async def _config(db, user: User, record_type="probate") -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Clallam probate", county="clallam",
        state="WA", record_type=record_type, fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, status="enriching", billed_at=None, **kw) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", billing_applied_at=billed_at, **kw))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, *, parcel="063000038700", prop=None, mailing=None, **kw) -> str:
    rid = str(uuid.uuid4())
    kw.setdefault("is_duplicate", False)
    kw.setdefault("dedup_hash", uuid.uuid4().hex)
    kw.setdefault("party_name", "ESTATE OF A TESTER")
    kw.setdefault("date_recorded", "11/02/2026")
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, parcel_id=parcel,
                  property_address=prop, mailing_address=mailing, **kw))
    await db.commit()
    return rid


async def _reason(db, rid) -> str | None:
    return (await db.execute(text(
        "SELECT enrichment_data->>:k FROM results WHERE id = :i"),
        {"k": DELIVERY_EXCLUDED_KEY, "i": rid})).scalar()


def _worker(fn):
    """Run a worker statement the way the worker does: the sync (psycopg2) system session."""
    with system_sync_session() as sdb:
        out = fn(sdb)
        sdb.commit()
    return out


async def _hold(db, job_id, user_id) -> int:
    await db.commit()
    return _worker(lambda s: mark_no_address_rows(s, job_id=job_id, user_id=str(user_id)))


async def _recover_mailing(db, rid):
    """What mailing_recovery does to a finished run's row: fill a NULL mailing address."""
    await db.execute(text("UPDATE results SET mailing_address = :m WHERE id = :i"),
                     {"m": RECOVERED_MAILING, "i": rid})
    await db.commit()


async def _count(db, job_id, condition) -> int:
    return (await db.execute(select(func.count()).select_from(Result).where(
        Result.job_id == job_id, condition))).scalar()


@pytest.mark.asyncio
async def test_hold_marks_only_no_address_survivors(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    no_situs = await _row(db, job_id, business_user.id)                    # parcel, no address
    unmatched = await _row(db, job_id, business_user.id, parcel=None)      # no parcel either
    lead = await _row(db, job_id, business_user.id, prop="1304 S LAUREL ST")
    mailing_only = await _row(db, job_id, business_user.id, mailing="PO BOX 9, SEQUIM, WA 98382")
    prior = await _row(db, job_id, business_user.id, is_duplicate=True, duplicate_reason="prior_run")

    assert await _hold(db, job_id, business_user.id) == 2
    assert [await _reason(db, r) for r in (no_situs, unmatched)] == [NO_ADDRESS, NO_ADDRESS]
    assert [await _reason(db, r) for r in (lead, mailing_only, prior)] == [None, None, None]


@pytest.mark.asyncio
async def test_a_recovered_address_does_not_make_a_held_row_a_lead(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    await _row(db, job_id, business_user.id, prop="1304 S LAUREL ST")
    held = await _row(db, job_id, business_user.id)
    await _hold(db, job_id, business_user.id)
    before = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    assert (before.new, before.no_address) == (1, 1)

    await _recover_mailing(db, held)

    # Not a lead, not billed, not exported: the billing partition and every list agree.
    assert await _count(db, job_id, actionable_condition()) == 1
    # The New leads list and its CSV: shown_condition AND the category (download_rows_select).
    assert await _count(db, job_id, and_(shown_condition(), category_condition("new"))) == 1
    assert await _count(db, job_id, list_condition("no_address")) == 1
    after = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    assert (after.new, after.no_address, after.unclassified) == (1, 1, 0)


@pytest.mark.asyncio
async def test_a_held_rows_same_run_siblings_are_held_with_it(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    h = uuid.uuid4().hex
    await _row(db, job_id, business_user.id, dedup_hash=h)
    sibling = await _row(db, job_id, business_user.id, prop="241756 HWY 101", dedup_hash=h,
                         is_duplicate=True, duplicate_reason="same_run")
    other = await _row(db, job_id, business_user.id, prop="9 OTHER RD",
                       is_duplicate=True, duplicate_reason="same_run")
    await _hold(db, job_id, business_user.id)
    assert await _reason(db, sibling) == NO_ADDRESS   # would deliver the property otherwise
    assert await _reason(db, other) is None


@pytest.mark.asyncio
async def test_a_rerun_decides_again(db, business_user):
    """A watchdog re-run that found the address before delivering does not stay held."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    await _hold(db, job_id, business_user.id)
    await db.execute(text("UPDATE results SET property_address = '83 SKY LN' WHERE id = :i"),
                     {"i": rid})
    await db.commit()
    assert await _hold(db, job_id, business_user.id) == 0
    assert await _reason(db, rid) is None


@pytest.mark.asyncio
async def test_the_cap_never_spends_quota_on_a_held_row(db, business_user):
    """An address found between the hold and the cap must not take this run's quota (Codex)."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    held = await _row(db, job_id, business_user.id, date_recorded=None)
    await _hold(db, job_id, business_user.id)
    await _recover_mailing(db, held)
    lead = await _row(db, job_id, business_user.id, prop="70 TWIN VIEW DR")
    capped = _worker(lambda s: mark_over_quota_rows(
        s, job_id=job_id, user_id=str(business_user.id), remaining=1, record_type="probate"))
    assert capped == []
    assert await _reason(db, held) == NO_ADDRESS and await _reason(db, lead) is None


@pytest.mark.asyncio
async def test_hold_never_touches_another_account(db, business_user, starter_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    theirs = await _row(db, job_id, starter_user.id)   # another account's row on the job id
    await _hold(db, job_id, business_user.id)
    assert await _reason(db, theirs) is None


@pytest.mark.asyncio
async def test_a_later_run_takes_the_claim_of_a_held_row(db, business_user):
    """The property can still be delivered, once, by the run that bills it."""
    cfg = await _config(db, business_user)
    parcel = "063000038700"
    h = legacy_strong_signature(parcel, None)
    assert h is not None
    old_job = await _job(db, business_user, cfg, status="done", billed_at=AFTER_RULE)
    held = await _row(db, old_job, business_user.id, parcel=parcel, dedup_hash=h,
                      enrichment_data={DELIVERY_EXCLUDED_KEY: NO_ADDRESS})
    db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=business_user.id, dedup_hash=h,
                           first_result_id=held, first_job_id=old_job, parcel_id=parcel))
    await db.commit()
    await _recover_mailing(db, held)
    assert await _count(db, old_job, actionable_condition()) == 0

    new_job = await _job(db, business_user, cfg)
    new_row = await _row(db, new_job, business_user.id, parcel=parcel, mailing=RECOVERED_MAILING,
                         dedup_hash=h, is_duplicate=True, duplicate_reason="prior_run",
                         duplicate_source_job_id=old_job)
    moved = _worker(lambda s: transfer_undelivered_claims(s, new_job, business_user.id, "probate"))
    assert moved == 1
    fresh = await db.get(Result, new_row)
    await db.refresh(fresh)
    assert fresh.is_duplicate is False
    part = await db.run_sync(lambda s: read_partition(s, new_job, business_user.id))
    assert part.new == 1
