"""Repairs after #289: truncated tax-bill mailings, the tax-bill check, em-dash party names.

The Assessor extract is a real zip in tmp_path (tests/test_king_rpacct_mailing.py helpers)
and the writes run against the real test database. The only substitute is King's live
tax-bill lookup (`batch_enrich_king_county`), which is a rate-limited county website.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_rpacct as kr
from tests.test_king_rpacct_mailing import _acct, _extract, _row, km


def _load(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load("king_taxbill_mailing_check")
dash = _load("repair_code_violation_party_name_dash")

TRUNCATED = "400 KC ADMIN BLDG/4TH AVE, STE #830"
TX = kr.Answer("found", "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051")


class TestTruncationPass:
    def test_an_unsourced_mailing_without_a_postal_code_is_repaired_from_the_extract(self):
        [d] = km.plan([_row(mailing_address=TRUNCATED)], {"1321400230": TX})
        assert (d["pass"], d["new_mail"]) == ("truncated_repair", TX.mailing_address)

    def test_a_recovery_sweep_value_is_still_a_truncation_candidate(self):
        # The sweep's tax-bill answers carry mailing_recovery_outcome but no mailing_source.
        [d] = km.plan([_row(mailing_address=TRUNCATED, recovery_outcome="found")], {"1321400230": TX})
        assert d["pass"] == "truncated_repair"

    def test_without_an_extract_answer_it_is_left_alone(self):
        [d] = km.plan([_row(mailing_address=TRUNCATED)], {"1321400230": kr.Answer("ambiguous")})
        assert (d["pass"], d["new_mail"]) == ("truncated_unresolved", None)

    def test_complete_or_sourced_addresses_are_not_truncated(self):
        for mail, src in (("PO BOX 1, KENT WA 98032", None), ("8745 HARVIE RD, SURREY BC V4N 1B1", None),
                          (TRUNCATED, "king_rpacct")):
            assert km.plan([_row(mailing_address=mail, mailing_source=src)], {"1321400230": TX}) == []


def _found(pid_mail: dict[str, str], lookup="verified"):
    return {p: {"mailing_address": m, "mailing_lookup": "found", "parcel_lookup": lookup}
            for p, m in pid_mail.items()}


class TestDecide:
    def _cand(self, kind="echo", pid="1321400230", mail=TRUNCATED):
        return {"row": SimpleNamespace(pid=pid, mailing_address=mail), "kind": kind,
                "extract_status": "ambiguous"}

    def test_a_verified_found_is_written(self):
        [d] = tb.decide([self._cand()], _found({"1321400230": "PO BOX 7, SEATTLE WA 98101"}))
        assert (d["write"], d["new_mail"], d["outcome"]) == (True, "PO BOX 7, SEATTLE WA 98101", "found")

    def test_a_recovered_or_mismatched_parcel_is_never_used(self):
        for lookup in ("recovered", "mismatch", None):
            [d] = tb.decide([self._cand()], _found({"1321400230": "PO BOX 7, SEATTLE WA 98101"}, lookup))
            assert (d["write"], d["new_mail"], d["outcome"]) == (False, None, "parcel_unverified")

    def test_none_is_stamped_and_unknown_is_left_for_a_later_run(self):
        none = {"1321400230": {"mailing_lookup": "none", "parcel_lookup": "verified"}}
        [d] = tb.decide([self._cand()], none)
        assert (d["write"], d["new_mail"]) == (True, None)
        for res in ({"1321400230": {"mailing_lookup": "error", "parcel_lookup": "verified"}}, {}):
            [d] = tb.decide([self._cand()], res)
            assert d["write"] is False


async def _row_in_job(db, user: User, *, record_type, status="done", parcel=None, mailing=None,
                      party="GAMBRELL RICHARD", ed=None) -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King repairs",
                           county="king", state="WA", record_type=record_type,
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=parcel,
                  property_address="506 S 330TH PL, FEDERAL WAY, WA 98003", mailing_address=mailing,
                  skip_trace_status="not_attempted", is_duplicate=False, enrichment_data=ed))
    await db.commit()
    return rid


@pytest.mark.asyncio
async def test_taxbill_check_fills_code_violations_and_repairs_truncations(
    db, business_user, tmp_path, monkeypatch,
):
    trunc = await _row_in_job(db, business_user, record_type="tax_delinquent",
                              parcel="1321400230", mailing=TRUNCATED)
    cv = await _row_in_job(db, business_user, record_type="code_violation",
                           ed={"kc_pin_status": "matched", "kc_pin": "0904000025", "record_number": "X-1"})
    gone = await _row_in_job(db, business_user, record_type="code_violation",
                             ed={"kc_pin_status": "matched", "kc_pin": "0904000099"})
    # Neither PIN is in the extract, so both go to the tax bill.
    zp = _extract(tmp_path, [_acct("111111", "1111", "1 OTHER ST", "KENT WA", "98032")])
    asked: list = []

    async def _king(pins, **kw):
        asked.append(sorted(pins))
        return {"1321400230": {"mailing_address": "400 KC ADMIN BLDG, STE #830, SEATTLE WA 98104",
                               "mailing_lookup": "found", "parcel_lookup": "verified"},
                "0904000025": {"mailing_address": "PO BOX 5003, BELLEVUE WA 98009",
                               "mailing_lookup": "found", "parcel_lookup": "echo_absent"},
                "0904000099": {"mailing_lookup": "none", "parcel_lookup": "verified"}}

    monkeypatch.setattr(kca, "batch_enrich_king_county", _king)

    def _answers(pins):
        accounts = kr.load_accounts(zp, pins)
        return {p: kr.resolve(accounts.get(p)) for p in pins}

    def _run(apply_writes):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return tb.run(sdb, _answers, apply_writes=apply_writes, report=tmp_path / "ev.jsonl", pace_s=0)

    dry = await asyncio.to_thread(_run, False)
    assert dry["would_write"] == 3 and "writes" not in dry
    stats = await asyncio.to_thread(_run, True)
    assert stats["writes"] == {"written": 3}

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, parcel_id, mailing_address, absentee_owner, enrichment_data FROM results "
        "WHERE id = ANY(:ids)"), {"ids": [trunc, cv, gone]})).all()}
    assert got[trunc].mailing_address == "400 KC ADMIN BLDG, STE #830, SEATTLE WA 98104"
    assert got[trunc].enrichment_data["mailing_source"] == "king_tax_bill"
    assert got[trunc].enrichment_data["mailing_repair_previous"] == TRUNCATED
    assert got[trunc].parcel_id == "1321400230"
    assert got[cv].mailing_address == "PO BOX 5003, BELLEVUE WA 98009"
    assert got[cv].parcel_id is None and got[cv].enrichment_data["record_number"] == "X-1"
    assert got[gone].mailing_address is None
    assert got[gone].enrichment_data["mailing_taxbill_outcome"] == "none"

    asked.clear()
    again = await asyncio.to_thread(_run, True)
    assert again["candidates"] == {} and asked == []


@pytest.mark.asyncio
async def test_taxbill_write_guard_skips_a_row_that_changed(db, business_user, monkeypatch):
    rid = await _row_in_job(db, business_user, record_type="tax_delinquent",
                            parcel="1321400230", mailing=TRUNCATED)

    async def _king(pins, **kw):
        # The row changes while the page is being read.
        from src.db.session import system_sync_session

        with system_sync_session() as s:
            s.execute(text("UPDATE results SET mailing_address = 'PO BOX 1, KENT WA 98032' WHERE id = :i"),
                      {"i": rid})
            s.commit()
        return _found({"1321400230": "PO BOX 7, SEATTLE WA 98101"})

    monkeypatch.setattr(kca, "batch_enrich_king_county", _king)

    def _run():
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return tb.run(sdb, lambda pins: {p: kr.Answer("absent") for p in pins},
                          apply_writes=True, report=None, pace_s=0)

    stats = await asyncio.to_thread(_run)
    assert stats["writes"] == {"skipped_by_write_guard": 1}
    assert (await db.execute(text("SELECT mailing_address FROM results WHERE id = :i"),
                             {"i": rid})).scalar_one() == "PO BOX 1, KENT WA 98032"


@pytest.mark.asyncio
async def test_party_name_dash_is_rewritten_only_on_finished_strong_identity_rows(db, business_user):
    done = await _row_in_job(db, business_user, record_type="code_violation", parcel="0904000025",
                             party="Land Use — 5412 39TH AVE W")
    running = await _row_in_job(db, business_user, record_type="code_violation", status="enriching",
                                parcel="0904000026", party="Weeds — 5412 39TH AVE W")
    other_type = await _row_in_job(db, business_user, record_type="probate", parcel="0904000027",
                                   party="SMITH — JONES")

    def _run(apply_writes):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return dash.run(sdb, apply_writes=apply_writes)

    dry = await asyncio.to_thread(_run, False)
    assert "writes" not in dry
    stats = await asyncio.to_thread(_run, True)
    assert stats["writes"]["written"] >= 1
    names = dict((await db.execute(text("SELECT id::text, party_name FROM results WHERE id = ANY(:ids)"),
                                   {"ids": [done, running, other_type]})).all())
    assert names[done] == "Land Use - 5412 39TH AVE W"
    assert names[running] == "Weeds — 5412 39TH AVE W"
    assert names[other_type] == "SMITH — JONES"


def test_new_name_formats():
    assert dash.new_name("Complaint — 2908 E HARRISON ST") == "Complaint - 2908 E HARRISON ST"
    assert dash.new_name("A—B") == "A-B"
