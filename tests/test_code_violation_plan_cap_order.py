"""Which code-violation leads the plan quota delivers: open cases first, the newest first.

King (Seattle SDCI) code violations arrive with no party_name and get an owner only for
exactly located parcels inside a time budget. Ranked by party_name, the delivered set
would be whichever rows happened to get a name before the budget ran out. The order is
instead fixed at scrape time: SDCI's settled statuses last, newest case first, id.

Real DB, the real ranking statement the job runs.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from src.db.models import Result
from tests.test_tax_plan_cap_order import _job, _mark, _over_quota

pytestmark = pytest.mark.asyncio


async def _cv(db, user, job_id, *, date: str | None, status: str | None,
              source: str = "seattle_sdci_code_violations", party: str | None = None,
              address: str | None = "7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
              duplicate: bool = False) -> str:
    rid = str(uuid.uuid4())
    ed = {"source": source}
    if status is not None:
        ed["status"] = status
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, date_recorded=date,
                  property_address=address, mailing_address=None, is_duplicate=duplicate,
                  duplicate_reason="prior_run" if duplicate else None, enrichment_data=ed))
    await db.commit()
    return rid


async def test_open_cases_first_then_newest_and_owner_names_do_not_matter(db, business_user):
    job_id = await _job(db, business_user, "code_violation")
    uid = str(business_user.id)
    # A named owner used to sort FIRST under party_name ordering.
    named_old = await _cv(db, business_user, job_id, date="08/13/2026",
                          status="Under Investigation", party="7011 ROOSEVELT WAY NE LLC 7")
    newest = await _cv(db, business_user, job_id, date="09/12/2026", status="Under Investigation")
    middle = await _cv(db, business_user, job_id, date="08/30/2026", status="Closed")
    completed_newest = await _cv(db, business_user, job_id, date="09/12/2026", status="Completed")
    open_dup = await _cv(db, business_user, job_id, date="09/11/2026", status="Open Duplicate")
    no_date = await _cv(db, business_user, job_id, date=None, status="Initiated")
    # Never ranked: no address, and a prior-run duplicate.
    no_address = await _cv(db, business_user, job_id, date="09/12/2026", status="Initiated",
                           address=None)
    duplicate = await _cv(db, business_user, job_id, date="09/12/2026", status="Initiated",
                          duplicate=True)

    capped = await asyncio.to_thread(_mark, job_id, uid, 3, "code_violation")

    # Delivered: the three open cases with dates, newest first. Held: undated open
    # case, then the settled ones.
    assert set(capped) == {no_date, completed_newest, open_dup}
    assert {newest, middle, named_old}.isdisjoint(capped)
    assert await _over_quota(db, job_id) == set(capped)
    assert no_address not in capped and duplicate not in capped

    again = await asyncio.to_thread(_mark, job_id, uid, 3, "code_violation")
    assert set(again) == set(capped)


async def test_tacoma_statuses_are_not_read_as_seattle_settled(db, business_user):
    job_id = await _job(db, business_user, "code_violation")
    uid = str(business_user.id)
    tacoma_completed_new = await _cv(db, business_user, job_id, date="09/12/2026",
                                     status="Completed", source="tacoma_code_violations")
    tacoma_open_old = await _cv(db, business_user, job_id, date="08/01/2026",
                                status="Open", source="tacoma_code_violations")

    capped = await asyncio.to_thread(_mark, job_id, uid, 1, "code_violation")

    assert capped == [tacoma_open_old]
    assert tacoma_completed_new not in capped


async def test_zero_remaining_marks_every_ranked_row(db, business_user):
    job_id = await _job(db, business_user, "code_violation")
    uid = str(business_user.id)
    a = await _cv(db, business_user, job_id, date="09/01/2026", status="Completed")
    b = await _cv(db, business_user, job_id, date="09/02/2026", status="Under Investigation")

    capped = await asyncio.to_thread(_mark, job_id, uid, 0, "code_violation")

    assert set(capped) == {a, b}
