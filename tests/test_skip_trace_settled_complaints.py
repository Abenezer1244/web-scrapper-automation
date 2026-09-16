"""Auto skip trace never pays for a code violation the city already settled.

A complaint Seattle SDCI closed as "Completed", or filed as an "Open Duplicate" of
another complaint, is not worth a paid Tracerfy lookup (owner decision 2026-09-13); nor
is a King County Accela case voided or closed with no violation. The list is per source
(src/scrapers/king_cv_sources.SETTLED_STATUSES), the same list the plan cap ranks by.
Real DB and real Redis; nothing is sent to Tracerfy here, the enqueue only writes
pending_skip_trace_rows.
"""
from __future__ import annotations

import os
import random
import uuid

import pytest
import redis as sync_redis
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio


async def _job(db, user: User, record_type: str) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Settled Complaint Config",
        county="king", state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []}, skip_trace_enabled=True,
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="enriching", trigger="manual"))
    await db.commit()
    return job_id


async def _lead(db, user: User, job_id: str, complaint_status: str | None,
                source: str = "seattle_sdci_code_violations") -> str:
    rid = str(uuid.uuid4())
    # A code violation is traceable only once enrichment named its owner from the county
    # (owner_source); without it the row is refused before the status gate is reached.
    pin = f"{random.randint(1, 9_999_999_999):010d}"
    parcel_id = None
    if source == "seattle_sdci_code_violations":
        ed = {"source": source, "record_number": f"{random.randint(1, 999999):06d}-26CP",
              "kc_pin": pin, "kc_pin_status": "matched", "kc_pin_source": "king_gis_point_in_parcel",
              "kc_pin_match": "exact", "owner_source": "king_erealproperty", "owner_pin": pin}
    elif source == "tacoma_code_violations":
        # Tacoma's owner proof is the Pierce ATIP pass, matched on the row's parcel_id.
        parcel_id = pin
        ed = {"source": source, "case_number": f"CV{random.randint(1, 999999):06d}",
              "owner_source": "pierce_atip", "owner_status": "matched", "owner_pin": pin}
    else:
        # Bellevue, Burien and King County Accela print the PIN into parcel_id at scrape.
        parcel_id = pin
        ed = {"source": source, "case_number": f"ENFCV{random.randint(1, 999999):06d}",
              "owner_source": "king_erealproperty", "owner_pin": pin}
    if complaint_status is not None:
        ed["status"] = complaint_status
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE", parcel_id=parcel_id,
        property_address=f"{random.randint(100, 99999)} MAIN ST, SEATTLE, WA 98101",
        enrichment_data=ed, skip_trace_status="not_attempted", is_duplicate=False,
    ))
    await db.commit()
    return rid


def _enqueue(job_id: str) -> None:
    from src.db.session import SyncSessionLocal
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    r = sync_redis.Redis.from_url(os.environ["REDIS_URL"])
    with SyncSessionLocal() as s:
        job = s.get(Job, job_id)
        config = s.get(ScraperConfig, job.scraper_config_id)
        _enqueue_skip_trace_rows(s, job, r, job_id, config)


async def _queued_for(db, job_id: str) -> set[str]:
    return {str(x) for x in (await db.execute(
        text("SELECT result_id FROM pending_skip_trace_rows WHERE job_id = :j"), {"j": job_id}
    )).scalars()}


@pytest.fixture
def skip_trace_on(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    # This file pins the STATUS gate. Tacoma rows here are named by Pierce ATIP, which
    # its own default-off switch keeps out of a paid lookup (test_pierce_cv_owner.py);
    # with it off every Tacoma assertion below would pass for that unrelated reason.
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)


async def test_completed_and_duplicate_complaints_are_not_queued(db, business_user, skip_trace_on):
    job_id = await _job(db, business_user, "code_violation")
    completed = await _lead(db, business_user, job_id, "Completed")
    duplicate = await _lead(db, business_user, job_id, "Open Duplicate")
    open_case = await _lead(db, business_user, job_id, "Under Investigation")
    closed = await _lead(db, business_user, job_id, "Closed")
    lowercase = await _lead(db, business_user, job_id, "completed")
    no_status = await _lead(db, business_user, job_id, None)

    _enqueue(job_id)

    queued = await _queued_for(db, job_id)
    assert completed not in queued and duplicate not in queued
    # Exact values only: "Closed", other casing and a missing status are still traced.
    assert {open_case, closed, lowercase, no_status} <= queued
    status = (await db.execute(text("SELECT skip_trace_status FROM results WHERE id = ANY(:ids)"),
                               {"ids": [completed, duplicate]})).scalars().all()
    assert status == ["not_attempted", "not_attempted"]


async def test_accela_voided_and_no_violation_cases_are_not_queued(db, business_user, skip_trace_on):
    job_id = await _job(db, business_user, "code_violation")
    accela = "kingco_accela_code_enforcement"
    settled = [await _lead(db, business_user, job_id, s, accela)
               for s in ("Void", "No Violation Found", "Case Opened No Violation Ltr",
                         "No Further Action Required")]
    open_case = await _lead(db, business_user, job_id, "Case Opened with Violation Ltr", accela)
    intake = await _lead(db, business_user, job_id, "Intake Processing", accela)
    bellevue_closed = await _lead(db, business_user, job_id, "Closed", "bellevue_code_enforcement")
    burien_closed = await _lead(db, business_user, job_id, "CLOSED", "burien_code_enforcement")
    # Settled words are scoped by source: Accela's on Bellevue, SDCI's on Accela and Burien
    # (the plan cap ranks these as ordinary cases, so they must be traced like one).
    bellevue_void = await _lead(db, business_user, job_id, "Void", "bellevue_code_enforcement")
    accela_completed = await _lead(db, business_user, job_id, "Completed", accela)
    burien_duplicate = await _lead(db, business_user, job_id, "Open Duplicate", "burien_code_enforcement")
    # Tacoma has no settled list (it reports "Open" / "Closed"): the plan cap ranks every
    # Tacoma case as ordinary, so every Tacoma case is traced, whatever its status word.
    tacoma_completed = await _lead(db, business_user, job_id, "Completed", "tacoma_code_violations")
    tacoma_closed = await _lead(db, business_user, job_id, "Closed", "tacoma_code_violations")

    _enqueue(job_id)

    queued = await _queued_for(db, job_id)
    assert set(settled).isdisjoint(queued)
    assert {open_case, intake, bellevue_closed, burien_closed, bellevue_void, accela_completed,
            burien_duplicate, tacoma_completed, tacoma_closed} <= queued


def test_settled_check_never_raises_on_malformed_json():
    from src.scrapers.king_cv_sources import is_settled, settled_sql
    from src.workers.tasks_helpers.enrich import _is_settled_complaint

    assert is_settled(["kingco_accela_code_enforcement"], "Void") is False
    assert is_settled("kingco_accela_code_enforcement", {"Void": 1}) is False
    assert _is_settled_complaint({"source": {"a": 1}, "status": "Void"}) is False
    assert _is_settled_complaint({"source": ["x"], "status": "Completed"}) is False
    assert _is_settled_complaint({"source": "seattle_sdci_code_violations", "status": "Completed"}) is True
    assert _is_settled_complaint(["not", "a", "dict"]) is False
    with pytest.raises(ValueError):
        settled_sql("enrichment_data) OR (1=1")


async def test_the_gate_is_scoped_to_code_violations(db, business_user, skip_trace_on):
    job_id = await _job(db, business_user, "tax_delinquent")
    lead = await _lead(db, business_user, job_id, "Completed")

    _enqueue(job_id)

    assert lead in await _queued_for(db, job_id)
