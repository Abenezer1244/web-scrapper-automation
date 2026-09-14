"""Repair of missing King property addresses on delivered leads (condo units + account numbers).

Extracts are real zip files built in tmp_path with the county's column layout (reused
from the enrichment tests). DB tests run against the real test database.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from tests.test_king_condo_unit_situs import G204, _acct, _condo_zip, _gis_row, _rpacct_zip, _unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_king_property_situs.py"
_spec = importlib.util.spec_from_file_location("repair_king_property_situs", _SCRIPT)
rk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rk  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(rk)

SNAPS = {"rpacct": "2026-09-05", "condo": "2026-09-14"}


def _cand(pid, *, source="king_landmark_json", resolved_by=None, resolved_pin=None, mailing=None):
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), job_id=uuid.uuid4(), raw_pid=pid, pid=pid,
                           mailing_address=mailing, property_city=None, property_state=None,
                           property_zip=None, row_source=source, resolved_by=resolved_by,
                           resolved_pin=resolved_pin)


class TestPlan:
    def _units(self):
        from src.scrapers.enrichment.king_condo_units import unit_situs

        return {"0268000490": unit_situs([G204]),
                "3110700100": unit_situs([_unit("311070", "0100", "1727 HARBOR AVE SW 98116", "98116", "206")]),
                "3268350090": unit_situs([_unit("326835", "0090", "", "", "8")])}

    def _gis(self):
        return {"0126039387": _gis_row("302 NW 203RD ST", "SHORELINE", "98177"),
                "0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007"),
                "3110700000": _gis_row("1727 HARBOR AVE SW", "SEATTLE", "98126")}

    def test_every_rule(self):
        rows = [_cand("0268000490"), _cand("012603938700"), _cand("3110700100"), _cand("3268350090"),
                _cand("192106911402", source="king_socrata"), _cand("9999999999"),
                _cand("012603938700", resolved_by="rpacct_account_number", resolved_pin="0126039387")]
        # The non-recorder 12-digit value IS a real account here: only its source excludes it.
        got = rk.plan(rows, {"012603938700": "0126039387", "192106911402": "1921069114"},
                      {**self._gis(), "1921069114": _gis_row("33713 186TH AVE SE", "AUBURN", "98092")},
                      self._units())
        assert [(d.outcome, d.address, d.resolved_pin) for d in got] == [
            ("fill_condo", "14527 NE 40TH ST #G204, BELLEVUE, WA 98007", None),
            ("fill_gis", "302 NW 203RD ST, SHORELINE, WA 98177", "0126039387"),
            ("unresolved:no_locality", None, None),
            ("unresolved:no_site_address", None, None),
            ("unresolved:no_usable_pin", None, None),      # 12 digits, not a recorder row
            ("unresolved:no_gis_or_condo_situs", None, None),
            ("fill_gis", "302 NW 203RD ST, SHORELINE, WA 98177", None),  # already resolved at job time
        ]

    def test_mailing_is_never_used_as_the_property(self):
        row = _cand("0268000490", mailing="14527 NE 40TH ST #G204, BELLEVUE, WA 98007")
        assert rk.plan([row], {}, {}, {})[0].address is None


# ─── database ────────────────────────────────────────────────────────────────

async def _job(db, user, *, county="king", status="done"):
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King prefc repair", county=county,
                           state="WA", record_type="pre_foreclosure", fields=["party_name"], enrichment=[],
                           schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status, trigger="manual",
               record_count=3, billed_count=3))
    await db.commit()
    return job_id


def _row(db, user, job_id, parcel, *, mailing=None):
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE", parcel_id=parcel,
                  property_address=None, mailing_address=mailing, skip_trace_status="not_attempted",
                  is_duplicate=False, dedup_hash=f"hash-{parcel}", source_fingerprint=f"fp-{parcel}",
                  enrichment_data={"source": "king_landmark_json", "instrument_number": "20260709000383"}))
    return rid


def _setup(tmp_path, monkeypatch):
    rp = _rpacct_zip(tmp_path, [_acct("012603938700", "012603", "9387", "302 NW 203RD ST", "SHORELINE  WA", "98177")])
    condo = _condo_zip(tmp_path, [G204])
    asked: list = []

    def _gis(parcel_ids, county, state, stats=None):
        asked.append(sorted(parcel_ids))
        table = {"0126039387": _gis_row("302 NW 203RD ST", "SHORELINE", "98177"),
                 "0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007")}
        return {p: dict(table[p]) for p in parcel_ids if p in table}

    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", _gis)
    return rp, condo, asked


def _run(rp, condo, *, apply_writes, report=None, job_id=None):
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        return rk.run(sdb, rp, condo, SNAPS, record_type="pre_foreclosure", job_id=job_id,
                      apply_writes=apply_writes, report=report)


async def _get(db, rid):
    return (await db.execute(text(
        "SELECT parcel_id, property_address, property_city, property_state, property_zip, mailing_address, "
        "absentee_owner, out_of_state_owner, owner_state, enrichment_data, dedup_hash, source_fingerprint, "
        "skip_trace_status FROM results WHERE id = :i"), {"i": rid})).first()


@pytest.mark.asyncio
async def test_dry_run_then_apply_then_rerun(db, business_user, tmp_path, monkeypatch):
    job_id = await _job(db, business_user)
    condo_row = _row(db, business_user, job_id, "0268000490", mailing="806 LAKESHORE DR, REDWOOD, CA 94065")
    acct_row = _row(db, business_user, job_id, "012603938700")
    running_job = await _job(db, business_user, status="enriching")
    running_row = _row(db, business_user, running_job, "0268000490")
    other_county = await _job(db, business_user, county="pierce")
    pierce_row = _row(db, business_user, other_county, "0268000490")
    await db.commit()
    rp, condo, _ = _setup(tmp_path, monkeypatch)
    report = tmp_path / "evidence.jsonl"

    dry = await asyncio.to_thread(_run, rp, condo, apply_writes=False, report=report, job_id=None)
    ours = [json.loads(line) for line in report.read_text().splitlines()
            if json.loads(line)["job_id"] in (job_id, running_job, other_county)]
    assert {e["result_id"] for e in ours} == {condo_row, acct_row}   # done King jobs only
    assert "writes" not in dry
    assert (await _get(db, condo_row)).property_address is None

    job_before = (await db.execute(text("SELECT billed_count, billing_applied_at, record_count FROM jobs "
                                        "WHERE id = :j"), {"j": job_id})).first()
    stats = await asyncio.to_thread(_run, rp, condo, apply_writes=True, job_id=job_id)
    assert stats["writes"] == {"written": 2}

    r = await _get(db, condo_row)
    assert r.property_address == "14527 NE 40TH ST #G204, BELLEVUE, WA 98007"
    assert (r.property_city, r.property_state, r.property_zip) == ("BELLEVUE", "WA", "98007")
    assert r.absentee_owner is True and r.out_of_state_owner is True and r.owner_state == "CA"
    assert r.enrichment_data["property_repair_reason"] == "king_condo_unit"
    assert r.enrichment_data["condo_unit_nbr"] == "G204"
    assert r.enrichment_data["instrument_number"] == "20260709000383"
    assert r.mailing_address == "806 LAKESHORE DR, REDWOOD, CA 94065"

    r = await _get(db, acct_row)
    assert r.parcel_id == "012603938700"
    assert (r.dedup_hash, r.source_fingerprint, r.skip_trace_status) == (
        "hash-012603938700", "fp-012603938700", "not_attempted")
    assert r.property_address == "302 NW 203RD ST, SHORELINE, WA 98177"
    assert r.enrichment_data["resolved_parcel_id"] == "0126039387"
    assert r.enrichment_data["resolved_by"] == "rpacct_account_number"
    assert r.enrichment_data["property_repair_reason"] == "king_account_number"

    assert (await _get(db, running_row)).property_address is None
    assert (await _get(db, pierce_row)).property_address is None
    job_after = (await db.execute(text("SELECT billed_count, billing_applied_at, record_count FROM jobs "
                                       "WHERE id = :j"), {"j": job_id})).first()
    assert tuple(job_after) == tuple(job_before)
    assert (await db.execute(text("SELECT count(*) FROM pending_skip_trace_rows WHERE job_id = :j"),
                             {"j": job_id})).scalar() == 0

    again = await asyncio.to_thread(_run, rp, condo, apply_writes=True, job_id=job_id)
    assert again["candidate_rows"] == 0 and again["writes"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_sql, expected_prop", [
    ("UPDATE results SET mailing_address = 'PO BOX 1, KENT, WA 98032' WHERE id = :i", None),
    # e.g. the live enrichment or a sweep filled it from eRealProperty meanwhile
    ("UPDATE results SET property_address = '14527 NE 40TH ST #G204 98007' WHERE id = :i",
     "14527 NE 40TH ST #G204 98007"),
])
async def test_a_row_changed_after_the_read_is_not_overwritten(
    db, business_user, tmp_path, monkeypatch, concurrent_sql, expected_prop,
):
    job_id = await _job(db, business_user)
    rid = _row(db, business_user, job_id, "0268000490")
    await db.commit()
    _, condo, _ = _setup(tmp_path, monkeypatch)

    def _go():
        from src.db.session import system_sync_session

        from src.scrapers.enrichment.king_condo_units import load_units, unit_situs

        with system_sync_session() as sdb:
            rows = sdb.execute(text(rk._CANDIDATES_SQL), {"record_type": "pre_foreclosure", "job_id": job_id}).all()
            units = {p: unit_situs(v) for p, v in load_units(condo, {"0268000490"}).items()}
            decisions = rk.plan(rows, {}, {"0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007")}, units)
            sdb.rollback()
            with system_sync_session() as other:
                other.execute(text(concurrent_sql), {"i": rid})
                other.commit()
            return rk.apply(sdb, decisions, SNAPS)

    counts = await asyncio.to_thread(_go)
    assert counts == {"skipped_by_write_guard": 1}
    assert (await _get(db, rid)).property_address == expected_prop
