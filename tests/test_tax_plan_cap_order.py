"""Which tax-delinquent leads the plan quota delivers: the largest balances first.

The plan cap ranked every record type by `party_name, date_recorded, id`. A tax
lead has no date, and King tax leads get their owner name from a slow one-page-
per-parcel lookup that reaches a few hundred of 16,000 parcels. So on job
b2f2ecd5 the 600 leads a customer paid for were whichever rows happened to get a
name (NULL names sort last), then random UUID order: a $14.71 balance could ship
while a $31,729.74 one was held back. Owner decision 2026-09-14: for tax
delinquent, deliver the largest balance first.

Real DB, the real ranking statement the job runs.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
from src.db.models import Job, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio


async def _job(db, user: User, record_type: str) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"cap order {record_type}",
        county="king", state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="enriching", trigger="manual"))
    await db.commit()
    return job_id


async def _row(db, user: User, job_id: str, *, parcel: str, amount: str | None = None,
               year: int | None = None, party: str | None = None,
               mailing: str | None = "PO BOX 1, KENT, WA 98032",
               duplicate: bool = False) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, parcel_id=parcel, party_name=party,
        delinquent_amount=Decimal(amount) if amount is not None else None,
        delinquent_bill_year=year, mailing_address=mailing, is_duplicate=duplicate,
        duplicate_reason="prior_run" if duplicate else None, enrichment_data={},
    ))
    await db.commit()
    return rid


def _mark(job_id: str, user_id: str, remaining: int, record_type: str) -> list[str]:
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.plan_cap import mark_over_quota_rows

    with system_sync_session() as sdb:
        ids = mark_over_quota_rows(sdb, job_id=job_id, user_id=user_id,
                                   remaining=remaining, record_type=record_type)
        sdb.commit()
        return ids


async def _over_quota(db, job_id: str) -> set[str]:
    rows = (await db.execute(text(
        "SELECT id FROM results WHERE job_id = :j AND enrichment_data->>:k = :v"),
        {"j": job_id, "k": DELIVERY_EXCLUDED_KEY, "v": OVER_QUOTA})).scalars()
    return {str(r) for r in rows}


async def test_tax_leads_are_delivered_largest_balance_first(db, business_user):
    import asyncio

    job_id = await _job(db, business_user, "tax_delinquent")
    uid = str(business_user.id)
    # Named with a small balance: under the old order this sorted FIRST.
    small_named = await _row(db, business_user, job_id, parcel="2624069024",
                             amount="14.71", year=2024, party="BEACH AVIVA")
    big = await _row(db, business_user, job_id, parcel="5379801941", amount="31729.74", year=2024)
    tie_newer = await _row(db, business_user, job_id, parcel="0040000055", amount="17181.80", year=2024)
    tie_older = await _row(db, business_user, job_id, parcel="0007200015", amount="17181.80", year=2023)
    no_amount = await _row(db, business_user, job_id, parcel="0872000090", amount=None)
    # Never ranked: no address (always shown, never billed) and a duplicate.
    no_address = await _row(db, business_user, job_id, parcel="0622079095", amount="99999.00",
                            mailing=None)
    duplicate = await _row(db, business_user, job_id, parcel="1111111111", amount="88888.00",
                           duplicate=True)

    capped = await asyncio.to_thread(_mark, job_id, uid, 3, "tax_delinquent")

    assert set(capped) == {small_named, no_amount}
    assert await _over_quota(db, job_id) == {small_named, no_amount}
    delivered = {big, tie_newer, tie_older}
    assert delivered.isdisjoint(capped)
    assert no_address not in capped and duplicate not in capped

    # One fewer slot: the tie breaks toward the OLDER delinquency, never at random.
    await db.execute(text("UPDATE results SET enrichment_data = '{}' WHERE job_id = :j"),
                     {"j": job_id})
    await db.commit()
    capped_two = await asyncio.to_thread(_mark, job_id, uid, 2, "tax_delinquent")
    assert set(capped_two) == {small_named, no_amount, tie_newer}


async def test_a_rerun_marks_the_same_rows(db, business_user):
    import asyncio

    job_id = await _job(db, business_user, "tax_delinquent")
    uid = str(business_user.id)
    for i, amount in enumerate(["500.00", "12.00", "9000.00", "77.00"]):
        await _row(db, business_user, job_id, parcel=f"12345{i:05d}", amount=amount, year=2025)

    first = await asyncio.to_thread(_mark, job_id, uid, 2, "tax_delinquent")
    second = await asyncio.to_thread(_mark, job_id, uid, 2, "tax_delinquent")

    assert set(first) == set(second) and len(first) == 2
    amounts = {r.amount for r in (await db.execute(text(
        "SELECT delinquent_amount AS amount FROM results WHERE id = ANY(:ids)"),
        {"ids": first})).all()}
    assert amounts == {Decimal("12.00"), Decimal("77.00")}


async def test_other_record_types_keep_their_order(db, business_user):
    import asyncio

    job_id = await _job(db, business_user, "pre_foreclosure")
    uid = str(business_user.id)
    b = await _row(db, business_user, job_id, parcel="2000000000", party="BRAVO", amount="1.00")
    a = await _row(db, business_user, job_id, parcel="1000000000", party="ALPHA", amount="1.00")
    c = await _row(db, business_user, job_id, parcel="3000000000", party="CHARLIE",
                   amount="999999.00")

    capped = await asyncio.to_thread(_mark, job_id, uid, 2, "pre_foreclosure")

    assert capped == [c]
    assert {a, b}.isdisjoint(capped)


def test_king_parcels_are_looked_up_in_the_order_the_cap_delivers():
    from src.workers.tasks_helpers.enrich import _tax_parcel_priority

    def _r(amount, year=2024, duplicate=False):
        return Result(delinquent_amount=Decimal(amount) if amount else None,
                      delinquent_bill_year=year, is_duplicate=duplicate)

    pid_map = {
        "0000000001": [_r("14.71")],
        "0000000002": [_r("31729.74")],
        "0000000003": [_r("17181.80", 2024)],
        "0000000004": [_r("17181.80", 2023)],
        "0000000005": [_r(None)],
        # Only a duplicate row: never delivered, so looked up last whatever it owes.
        "0000000006": [_r("99999.00", duplicate=True)],
        # A delivered row and a duplicate: the delivered row's balance counts.
        "0000000007": [_r("50.00"), _r("88888.00", duplicate=True)],
    }

    assert _tax_parcel_priority(pid_map) == [
        "0000000002", "0000000004", "0000000003", "0000000007", "0000000001",
        "0000000005", "0000000006",
    ]
