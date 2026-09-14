"""The historical King tax owner repair only queues leads for the owner sweep.

It never looks anything up and never writes a name: it marks eligible delivered
leads `owner_lookup_deferred` so src/workers/owner_recovery.py picks them up.
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

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "requeue_king_tax_owner_recovery.py"
_spec = importlib.util.spec_from_file_location("requeue_king_tax_owner_recovery", _SCRIPT)
rq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rq)


async def _job(db, user: User, *, county="king", record_type="tax_delinquent", status="done") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="owner requeue", county=county, state="WA",
        record_type=record_type, fields=["party_name"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user, job_id, *, parcel="0007200015", party=None, enrichment=None,
               duplicate=False) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=parcel,
                  delinquent_amount="7066.18", mailing_address="5140 S 172ND LANE, SEATAC, WA 98188",
                  enrichment_data=enrichment if enrichment is not None else {"source": "king"},
                  skip_trace_status="not_attempted", is_duplicate=duplicate,
                  duplicate_reason="prior_run" if duplicate else None))
    await db.commit()
    return rid


def _run(apply: bool, tmp_path: Path) -> dict:
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        return rq.requeue(db, apply=apply, report=tmp_path / "evidence.jsonl")


async def test_only_delivered_unnamed_king_tax_leads_are_queued(db, business_user, tmp_path):
    king = await _job(db, business_user)
    eligible = await _row(db, business_user, king)
    skipped = [
        await _row(db, business_user, king, parcel="1000000001", party="SHAN HOMES2 LLC"),
        await _row(db, business_user, king, parcel="1000000002", duplicate=True),
        await _row(db, business_user, king, parcel="1000000003",
                   enrichment={"delivery_excluded_reason": "over_quota"}),
        await _row(db, business_user, king, parcel="1000000004",
                   enrichment={"owner_lookup_outcome": "not_on_record"}),
        await _row(db, business_user, king, parcel="1000000005",
                   enrichment={"owner_recovery_outcome": "gave_up"}),
        await _row(db, business_user, king, parcel="012603938700"),
        await _row(db, business_user, await _job(db, business_user, status="enriching"),
                   parcel="1000000006"),
        await _row(db, business_user, await _job(db, business_user, record_type="pre_foreclosure"),
                   parcel="1000000007"),
        await _row(db, business_user, await _job(db, business_user, county="snohomish"),
                   parcel="1000000008"),
    ]
    billed_before = (await db.execute(text("SELECT billed_count FROM jobs WHERE id = :j"),
                                      {"j": king})).scalar()

    dry = await asyncio.to_thread(_run, False, tmp_path)
    assert dry["candidates"] == 1 and dry["marked"] == 0
    assert "owner_lookup_deferred" not in (await db.execute(
        text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": eligible})).scalar()

    applied = await asyncio.to_thread(_run, True, tmp_path)
    assert applied["marked"] == 1

    row = (await db.execute(text(
        "SELECT party_name, enrichment_data, skip_trace_status FROM results WHERE id = :i"),
        {"i": eligible})).first()
    assert row.party_name is None and row.skip_trace_status == "not_attempted"
    assert row.enrichment_data["owner_lookup_deferred"] is True
    assert row.enrichment_data["owner_lookup_deferred_reason"] == rq.REASON
    assert row.enrichment_data["source"] == "king"
    for rid in skipped:
        ed = (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"),
                               {"i": rid})).scalar() or {}
        assert ed.get("owner_lookup_deferred") is not True
    assert (await db.execute(text("SELECT billed_count FROM jobs WHERE id = :j"),
                             {"j": king})).scalar() == billed_before

    again = await asyncio.to_thread(_run, True, tmp_path)
    assert again["candidates"] == 0 and again["marked"] == 0


async def test_a_queued_lead_is_one_the_owner_sweep_selects(db, business_user, tmp_path):
    from src.workers import owner_recovery as orc

    job_id = await _job(db, business_user)
    await _row(db, business_user, job_id, parcel="1000000099")
    await asyncio.to_thread(_run, True, tmp_path)

    def _selected() -> list[str]:
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return [r.parcel_id for r in sdb.execute(
                text(orc._CANDIDATE_PARCELS_SQL),
                {"max_attempts": orc._MAX_ATTEMPTS, "batch": 500}).all()]

    assert "1000000099" in await asyncio.to_thread(_selected)
