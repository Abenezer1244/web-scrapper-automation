"""The historical repair only queues rows for recovery. It never looks up or writes an address.

Real DB, real rows, the real script entry point.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _licensed_mailing_enabled(monkeypatch):
    """These tests exercise the county-GIS mailing mechanism itself, which ships
    switched OFF for license-restricted layers (Snohomish, Cowlitz) until counsel
    clears commercial use. Turn it on here; test_county_gis_license_gate.py pins
    the default-off behaviour."""
    from src.config import settings

    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "requeue_gis_mailing_recovery.py"
_spec = importlib.util.spec_from_file_location("requeue_gis_mailing_recovery", _SCRIPT)
rq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rq)


async def _job(db, user: User, county: str, status: str = "done") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} requeue",
        county=county, state="WA", record_type="probate", fields=["party_name"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user, job_id, *, parcel="08931001", mailing=None, enrichment=None) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="BURPEE JON",
                  parcel_id=parcel, property_address="3738 PENNSYLVANIA ST",
                  mailing_address=mailing, enrichment_data=enrichment,
                  skip_trace_status="hit", phone="3605550100", is_duplicate=False))
    await db.commit()
    return rid


async def _ed(db, rid):
    return (await db.execute(text(
        "SELECT enrichment_data, mailing_address, phone, skip_trace_status, property_address "
        "FROM results WHERE id = :i"), {"i": rid})).first()


def _run(counties, apply, tmp_path):
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        return rq.requeue(sdb, counties, apply=apply, report=tmp_path / "evidence.jsonl")


async def test_dry_run_writes_nothing_but_reports_every_candidate(db, business_user, tmp_path):
    job_id = await _job(db, business_user, "cowlitz")
    rid = await _row(db, business_user, job_id)

    stats = await asyncio.to_thread(_run, ["cowlitz"], False, tmp_path)

    assert stats["candidates"] == 1 and stats["marked"] == 0
    assert (await _ed(db, rid)).enrichment_data is None
    assert rid in (tmp_path / "evidence.jsonl").read_text(encoding="utf-8")


async def test_apply_marks_only_eligible_rows_and_changes_nothing_else(db, business_user,
                                                                       tmp_path):
    done = await _job(db, business_user, "snohomish")
    live = await _job(db, business_user, "snohomish", status="enriching")
    eligible = await _row(db, business_user, done, parcel="00522400008900",
                          enrichment={"situs_note": "kept"})
    has_mail = await _row(db, business_user, done, parcel="00647500007600",
                          mailing="9403 13TH PL SE, EVERETT, WA 98208")
    concluded = await _row(db, business_user, done, parcel="30072900302800",
                           enrichment={"mailing_recovery_outcome": "none",
                                       "mailing_lookup_deferred": False})
    on_live_job = await _row(db, business_user, live, parcel="00583100000300")
    short_parcel = await _row(db, business_user, done, parcel="123")
    phone_before = (await _ed(db, eligible)).phone

    stats = await asyncio.to_thread(_run, ["snohomish"], True, tmp_path)

    assert stats["marked"] == 1
    row = await _ed(db, eligible)
    assert row.enrichment_data["mailing_lookup_deferred"] is True
    assert row.enrichment_data["mailing_requeue_reason"] == rq.REASON
    assert row.enrichment_data["situs_note"] == "kept"
    # Contact data, property address and skip-trace state are untouched, and no address
    # is written by the repair itself.
    assert (row.mailing_address, row.skip_trace_status, row.property_address) == (
        None, "hit", "3738 PENNSYLVANIA ST")
    # phone is encrypted at rest, so compare the stored ciphertext is unchanged.
    assert row.phone == phone_before
    for rid in (has_mail, concluded, on_live_job, short_parcel):
        ed = (await _ed(db, rid)).enrichment_data or {}
        assert ed.get("mailing_lookup_deferred") is not True


async def test_rerunning_is_a_no_op(db, business_user, tmp_path):
    job_id = await _job(db, business_user, "cowlitz")
    await _row(db, business_user, job_id)

    first = await asyncio.to_thread(_run, ["cowlitz"], True, tmp_path)
    second = await asyncio.to_thread(_run, ["cowlitz"], True, tmp_path)

    assert first["marked"] == 1
    assert second["candidates"] == 0 and second["marked"] == 0


async def test_a_county_without_a_mailing_source_is_refused(tmp_path):
    with pytest.raises(ValueError, match="clark"):
        await asyncio.to_thread(_run, ["clark"], True, tmp_path)


async def test_a_json_null_row_is_queued_where_the_sweep_can_see_it_and_recovers(
    db, business_user, tmp_path, monkeypatch,
):
    """ORM rows created with enrichment_data=None store JSON null. Merging into that
    with `||` built an array, hiding the marker from every `->>` read."""
    from src.scrapers.enrichment import county_gis as cg
    from src.workers import mailing_recovery as mr

    job_id = await _job(db, business_user, "cowlitz")
    rid = await _row(db, business_user, job_id, parcel="2305201")
    await asyncio.to_thread(_run, ["cowlitz"], True, tmp_path)
    kind = (await db.execute(text(
        "SELECT jsonb_typeof(enrichment_data::jsonb) FROM results WHERE id = :i"),
        {"i": rid})).scalar()
    assert kind == "object"

    mail = "PO BOX 1, KELSO, WA 98626"
    monkeypatch.setattr(cg, "batch_enrich_parcels_gis", lambda pids, county, state, stats=None: (
        stats.update(county_unreached=[]) if stats is not None else None) or {
            p: {"mailing_address": mail} for p in pids})
    stats = await asyncio.to_thread(mr.recover_deferred_gis_mailing)

    assert stats["found"] == 1
    assert (await _ed(db, rid)).mailing_address == mail
