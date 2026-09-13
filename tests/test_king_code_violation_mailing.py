"""King code violations: strict parcel location from coordinates, then extract mailing.

Only King's parcel-layer HTTP response is substituted (feature payloads copied from
real answers for these addresses on 2026-09-13). Matching rules, extract resolution and
the DB writes run for real.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment import king_rpacct as kr
from tests.test_king_rpacct_mailing import _acct, _extract

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_king_code_violation_mailing.py"
_spec = importlib.util.spec_from_file_location("backfill_king_cv", _SCRIPT)
bk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bk)


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _layer(monkeypatch, *features):
    monkeypatch.setattr(kpl, "safe_get", lambda *a, **kw: _Resp(
        {"features": [{"attributes": f} for f in features]}))


P0904 = {"PIN": "0904000025", "ADDR_FULL": "5412 39TH AVE W", "ZIP5": "98199"}


class TestLocate:
    def test_one_parcel_with_the_same_street_and_zip_matches(self, monkeypatch):
        _layer(monkeypatch, P0904)
        loc = kpl.locate("47.66817947", "-122.40862917", "5412 39TH AVE W, SEATTLE WA 98199")
        assert (loc.status, loc.pin) == ("matched", "0904000025")

    def test_spelled_out_directionals_and_suffixes_still_match(self, monkeypatch):
        _layer(monkeypatch, {"PIN": "7440000001", "ADDR_FULL": "7440 W MARGINAL WAY S", "ZIP5": "98108"})
        loc = kpl.locate(47.5, -122.3, "7440 WEST MARGINAL WAY SOUTH, SEATTLE WA 98108")
        assert loc.status == "matched"

    def test_a_house_number_suffix_is_a_different_address(self, monkeypatch):
        _layer(monkeypatch, {"PIN": "2346000001", "ADDR_FULL": "2346A FRANKLIN AVE E", "ZIP5": "98102"})
        assert kpl.locate(47.6, -122.3, "2346 FRANKLIN AVE E, SEATTLE WA 98102").status == "address_mismatch"

    def test_a_corner_lot_on_the_other_street_is_rejected(self, monkeypatch):
        _layer(monkeypatch, {"PIN": "4708000001", "ADDR_FULL": "4708 NE 68TH ST", "ZIP5": "98115"})
        assert kpl.locate(47.6, -122.3, "6804 47TH AVE NE, SEATTLE WA 98115").status == "address_mismatch"

    def test_overlapping_condo_polygons_are_never_guessed_between(self, monkeypatch):
        _layer(monkeypatch, P0904, {**P0904, "PIN": "0904000026"})
        assert kpl.locate(47.6, -122.4, "5412 39TH AVE W, SEATTLE WA 98199").status == "multiple"

    def test_a_different_zip_is_rejected(self, monkeypatch):
        _layer(monkeypatch, P0904)
        assert kpl.locate(47.6, -122.4, "5412 39TH AVE W, SEATTLE WA 98107").status == "address_mismatch"

    def test_bad_coordinates_and_service_errors_are_errors(self, monkeypatch):
        assert kpl.locate("abc", None, "x").status == "error"
        monkeypatch.setattr(kpl, "safe_get", lambda *a, **kw: _Resp({"error": {"code": 500}}))
        assert kpl.locate(47.6, -122.4, "5412 39TH AVE W").status == "error"


def test_a_transient_error_leaves_no_status_so_the_row_retries(monkeypatch):
    monkeypatch.setattr(kpl, "safe_get", lambda *a, **kw: _Resp({"error": {"code": 500}}))
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    decisions, _ = kpl.resolve_code_violation_mailing([("r1", 47.6, -122.4, "5412 39TH AVE W")])
    assert decisions == {}


def test_matched_parcel_gets_extract_mailing_and_one_lookup_per_point(monkeypatch, tmp_path):
    calls: list = []

    def _get(*a, **kw):
        calls.append(kw["params"]["geometry"])
        return _Resp({"features": [{"attributes": P0904}]})

    monkeypatch.setattr(kpl, "safe_get", _get)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    zp = _extract(tmp_path, [_acct("090400", "0025", "PO BOX 5003", "BELLEVUE WA", "98009")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (zp, "2026-09-05"))

    decisions, snap = kpl.resolve_code_violation_mailing([
        ("a", "47.66817947", "-122.40862917", "5412 39TH AVE W, SEATTLE WA 98199"),
        ("b", "47.66817947", "-122.40862917", "5412 39TH AVE W, SEATTLE WA 98199"),
    ])
    assert len(calls) == 1 and snap == "2026-09-05"
    for key in ("a", "b"):
        assert decisions[key]["kc_pin"] == "0904000025"
        assert decisions[key]["mailing_address"] == "PO BOX 5003, BELLEVUE, WA 98009"


async def _cv_row(db, user: User, *, status="done", parcel=None, mailing=None) -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King CV",
                           county="king", state="WA", record_type="code_violation",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="Weeds 5412 39TH AVE W",
                  parcel_id=parcel, property_address="5412 39TH AVE W, SEATTLE WA 98199",
                  mailing_address=mailing, skip_trace_status="not_attempted", is_duplicate=False,
                  enrichment_data={"latitude": "47.66817947", "longitude": "-122.40862917",
                                   "record_number": "000630-26CP"}))
    await db.commit()
    return rid


@pytest.mark.asyncio
async def test_backfill_fills_done_rows_keeps_parcel_id_null_and_converges(
    db, business_user, tmp_path, monkeypatch,
):
    done = await _cv_row(db, business_user)
    live = await _cv_row(db, business_user, status="enriching")
    _layer(monkeypatch, P0904)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    zp = _extract(tmp_path, [_acct("090400", "0025", "PO BOX 5003", "BELLEVUE WA", "98009")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (zp, "2026-09-05"))

    def _run(apply_writes):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return bk.run(sdb, apply_writes=apply_writes, limit=None,
                          report=tmp_path / "ev.jsonl", pace_s=0)

    dry = await asyncio.to_thread(_run, False)
    assert dry["candidates"] == 1 and "writes" not in dry
    stats = await asyncio.to_thread(_run, True)
    assert stats["writes"] == {"written": 1, "skipped_by_write_guard": 0}

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, parcel_id, mailing_address, owner_state, enrichment_data FROM results "
        "WHERE id = ANY(:ids)"), {"ids": [done, live]})).all()}
    assert got[done].mailing_address == "PO BOX 5003, BELLEVUE, WA 98009"
    assert got[done].parcel_id is None
    assert got[done].enrichment_data["kc_pin"] == "0904000025"
    assert got[done].enrichment_data["record_number"] == "000630-26CP"
    assert got[live].mailing_address is None

    again = await asyncio.to_thread(_run, True)
    assert again["candidates"] == 0


def test_a_unit_address_is_never_matched_to_the_base_parcel(monkeypatch):
    _layer(monkeypatch, P0904)
    assert kpl.locate(47.6, -122.4, "5412 39TH AVE W #6, SEATTLE WA 98199").status == "unit_address"
    for addr in ("5412 39TH AVE W UNIT 6, SEATTLE WA 98199", "5412 39TH AVE W, APT 6, SEATTLE WA 98199",
                 "5412 39TH AVE W Apt. 6, SEATTLE WA 98199", "5412 39TH AVE W Unit-6, SEATTLE WA 98199"):
        assert kpl.locate(47.6, -122.4, addr).status == "unit_address", addr


def test_a_truncated_response_is_not_one_polygon(monkeypatch):
    monkeypatch.setattr(kpl, "safe_get", lambda *a, **kw: _Resp(
        {"features": [{"attributes": P0904}], "exceededTransferLimit": True}))
    assert kpl.locate(47.6, -122.4, "5412 39TH AVE W, SEATTLE WA 98199").status == "multiple"


def test_an_unavailable_extract_leaves_matched_rows_retryable(monkeypatch):
    _layer(monkeypatch, P0904)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: None)
    decisions, snap = kpl.resolve_code_violation_mailing(
        [("a", 47.6, -122.4, "5412 39TH AVE W, SEATTLE WA 98199")])
    assert decisions == {} and snap is None
