"""King mailing from the Assessor bulk extract: the reader, the decision rules, the writes.

The extract is a real zip file built in tmp_path with the county's real column layout and
rows copied from the 2026-09-05 file. The DB tests run against the real test database.
"""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_rpacct as kr

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "king_rpacct_mailing.py"
_spec = importlib.util.spec_from_file_location("king_rpacct_mailing", _SCRIPT)
km = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(km)

COLUMNS = ["AcctNbr", "Major", "Minor", "AttnLine", "AddrLine", "CityState", "ZipCode",
           "LevyCode", "TaxStat", "BillYr", "NewConstructionFlag", "TaxValReason",
           "ApprLandVal", "ApprImpsVal", "TaxableLandVal", "TaxableImpsVal"]


def _extract(tmp_path: Path, rows: list[dict]) -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in COLUMNS})
    path = tmp_path / "rpacct.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EXTR_RPAcct_NoName.csv", buf.getvalue().encode("latin-1"))
    return path


def _acct(major, minor, addr, city_state, zipcode, attn=""):
    return {"Major": major, "Minor": minor, "AddrLine": addr, "CityState": city_state,
            "ZipCode": zipcode, "AttnLine": attn}


# ─── reader ──────────────────────────────────────────────────────────────────

class TestFormatAndResolve:
    def test_padded_city_state(self):
        assert kr.format_mailing({"AddrLine": "2835 140TH AVE NE", "CityState": "BELLEVUE  WA",
                                  "ZipCode": "98005     "}) == "2835 140TH AVE NE, BELLEVUE, WA 98005"

    def test_nine_digit_zip_and_po_box(self):
        assert kr.format_mailing({"AddrLine": "PO BOX 961089", "CityState": "FORT WORTH TX",
                                  "ZipCode": "761610089"}) == "PO BOX 961089, FORT WORTH, TX 76161-0089"

    def test_foreign_address_keeps_country_and_drops_zero_zip(self):
        assert kr.format_mailing({"AddrLine": "8745 HARVIE RD SURREY BC V4N", "CityState": "CANADA",
                                  "ZipCode": "00000"}) == "8745 HARVIE RD SURREY BC V4N, CANADA"

    def test_empty_or_placeholder_street_is_no_address(self):
        assert kr.format_mailing({"AddrLine": "   ", "CityState": "SEATTLE WA", "ZipCode": "98101"}) is None
        assert kr.format_mailing({"AddrLine": "UNKNOWN", "CityState": "SEATTLE WA", "ZipCode": "98101"}) is None

    def test_attention_line_is_never_part_of_the_address(self):
        row = _acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051",
                    attn="GAMBRELL RICHARD V JR TTEE")
        assert kr.format_mailing(row) == "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051"

    def test_two_accounts_naming_different_addresses_are_ambiguous(self):
        a = _acct("145360", "1063", "12541 A 35TH AVE NE", "SEATTLE WA", "98125")
        b = _acct("145360", "1063", "560 NACHES AVE SW #110", "RENTON WA", "98057")
        assert kr.resolve([a, b]) == kr.Answer("ambiguous")

    def test_two_accounts_naming_the_same_address_are_one_answer(self):
        a = _acct("000020", "0006", "2835 140TH AVE NE", "BELLEVUE  WA", "98005")
        b = _acct("000020", "0006", "2835 140TH AVE NE", "BELLEVUE WA", "98005-1234")
        assert kr.resolve([a, b]).status == "found"

    def test_same_street_in_two_cities_is_ambiguous(self):
        a = _acct("100000", "0001", "100 MAIN ST", "KENT WA", "98032")
        b = _acct("100000", "0001", "100 MAIN ST", "AUBURN WA", "98002")
        assert kr.resolve([a, b]) == kr.Answer("ambiguous")

    def test_malformed_major_minor_never_matches(self):
        assert kr.pin_of("12A456", "0001") is None
        assert kr.pin_of("1234567", "0001") is None
        assert kr.pin_of("000020", "0001") == "0000200001"

    def test_absent_and_no_address(self):
        assert kr.resolve(None) == kr.Answer("absent")
        assert kr.resolve([_acct("1", "2", "", "SEATTLE WA", "98101")]) == kr.Answer("no_address")


class TestLoad:
    def test_loads_only_requested_pins_with_leading_zeros(self, tmp_path):
        path = _extract(tmp_path, [
            _acct("000020", "0001", "PO BOX 961089", "FORT WORHT TX", "76160"),
            _acct("128230", "1809", "3011 S ESTELLE ST", "SEATTLE WA", "98144"),
        ])
        out = kr.load_accounts(path, {"1282301809"})
        assert list(out) == ["1282301809"]
        assert kr.resolve(out["1282301809"]).mailing_address == "3011 S ESTELLE ST, SEATTLE, WA 98144"

    def test_a_changed_schema_fails_loudly(self, tmp_path):
        path = tmp_path / "bad.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("EXTR_RPAcct_NoName.csv", "AcctNbr,Major,Minor,Street\n1,2,3,4\n")
        with pytest.raises(RuntimeError, match="schema changed"):
            kr.load_accounts(path, {"0000020003"})


# ─── decision rules ──────────────────────────────────────────────────────────

def _row(**kw):
    base = {"id": "r", "user_id": "u", "pid": "1321400230", "raw_pid": "1321400230",
            "property_address": "506 S 330TH PL, FEDERAL WAY, WA 98003", "property_city": None,
            "property_state": None, "property_zip": None,
            "mailing_address": "506 S 330TH PL, FEDERAL WAY, WA 98003-5900",
            "mailing_source": None, "recovery_outcome": None, "deferred": False}
    base.update(kw)
    return SimpleNamespace(**base)


TX = kr.Answer("found", "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051")


class TestPlan:
    def test_the_situs_echo_is_replaced_by_the_county_address(self):
        [d] = km.plan([_row()], {"1321400230": TX})
        assert (d["pass"], d["new_mail"]) == ("echo_repair", TX.mailing_address)

    def test_an_unresolvable_echo_is_left_alone_never_nulled(self):
        [d] = km.plan([_row()], {"1321400230": kr.Answer("ambiguous")})
        assert (d["pass"], d["new_mail"]) == ("echo_unresolved", None)

    def test_a_tax_bill_formatted_value_is_not_an_echo(self):
        # The real tax-bill parser writes "STREET, CITY ST ZIP" (no comma before the state).
        row = _row(mailing_address="506 S 330TH PL, FEDERAL WAY WA 98003")
        assert km.plan([row], {"1321400230": TX}) == []

    def test_a_verified_source_is_never_second_guessed(self):
        for kw in ({"mailing_source": "king_assessor_tax_bill"}, {"recovery_outcome": "found"}):
            assert km.plan([_row(**kw)], {"1321400230": TX}) == []

    def test_a_real_different_mailing_is_left_alone(self):
        row = _row(mailing_address="PO BOX 1, KENT, WA 98032")
        assert km.plan([row], {"1321400230": TX}) == []

    def test_deferred_rows_fill_only_on_an_unambiguous_answer(self):
        row = _row(mailing_address=None, deferred=True)
        assert km.plan([row], {"1321400230": kr.Answer("ambiguous")}) == []
        [d] = km.plan([row], {"1321400230": TX})
        assert d["pass"] == "deferred_fill"


# ─── writes, real DB ─────────────────────────────────────────────────────────

async def _king_row(db, user: User, *, status="done", mailing, deferred=False, parcel="1321400230"):
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King rpacct",
                           county="king", state="WA", record_type="tax_delinquent",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="GAMBRELL RICHARD",
                  parcel_id=parcel, property_address="506 S 330TH PL, FEDERAL WAY, WA 98003",
                  mailing_address=mailing, skip_trace_status="hit", is_duplicate=False,
                  enrichment_data={"mailing_lookup_deferred": True} if deferred else None))
    await db.commit()
    return rid, job_id


def _run(zip_path, apply_writes, tmp_path):
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        return km.run(sdb, zip_path, "2026-09-05", apply_writes=apply_writes,
                      report=tmp_path / "evidence.jsonl")


@pytest.mark.asyncio
async def test_apply_repairs_the_echo_and_recomputes_flags_without_touching_billing(
    db, business_user, tmp_path,
):
    rid, job_id = await _king_row(db, business_user,
                                  mailing="506 S 330TH PL, FEDERAL WAY, WA 98003-5900")
    path = _extract(tmp_path, [_acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")])
    job_before = (await db.execute(text(
        "SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"), {"j": job_id})).first()

    dry = await asyncio.to_thread(_run, path, False, tmp_path)
    assert dry["decisions"] == 1 and "writes" not in dry
    stats = await asyncio.to_thread(_run, path, True, tmp_path)

    assert stats["writes"] == {"written": 1}
    row = (await db.execute(text(
        "SELECT mailing_address, owner_state, out_of_state_owner, absentee_owner, "
        "enrichment_data, skip_trace_status, parcel_id, property_address FROM results WHERE id = :i"),
        {"i": rid})).first()
    assert row.mailing_address == "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051"
    assert (row.owner_state, row.out_of_state_owner, row.absentee_owner) == ("TX", True, True)
    assert row.enrichment_data["mailing_source"] == "king_rpacct"
    assert row.enrichment_data["mailing_rpacct_snapshot"] == "2026-09-05"
    assert row.enrichment_data["mailing_repair_previous"] == "506 S 330TH PL, FEDERAL WAY, WA 98003-5900"
    assert (row.skip_trace_status, row.parcel_id) == ("hit", "1321400230")
    assert row.property_address == "506 S 330TH PL, FEDERAL WAY, WA 98003"
    assert tuple((await db.execute(text(
        "SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"), {"j": job_id})).first()) == tuple(job_before)

    again = await asyncio.to_thread(_run, path, True, tmp_path)
    assert again["decisions"] == 0  # the repaired row now carries a verified source


@pytest.mark.asyncio
async def test_deferred_rows_are_filled_and_live_jobs_are_skipped(db, business_user, tmp_path):
    done, _ = await _king_row(db, business_user, mailing=None, deferred=True)
    live, _ = await _king_row(db, business_user, mailing=None, deferred=True, status="enriching")
    path = _extract(tmp_path, [_acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")])

    await asyncio.to_thread(_run, path, True, tmp_path)

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, mailing_address, enrichment_data FROM results WHERE id = ANY(:ids)"),
        {"ids": [done, live]})).all()}
    assert got[done].mailing_address.startswith("2736 ROSECLIFF")
    assert got[done].enrichment_data["mailing_lookup_deferred"] is False
    assert got[live].mailing_address is None


@pytest.mark.asyncio
async def test_a_row_that_changed_since_the_read_is_not_overwritten(db, business_user, tmp_path):
    rid, _ = await _king_row(db, business_user, mailing="506 S 330TH PL, FEDERAL WAY, WA 98003-5900")
    path = _extract(tmp_path, [_acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")])

    def _race():
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.king_rpacct import load_accounts, resolve

        with system_sync_session() as sdb:
            rows = sdb.execute(text(km._CANDIDATES_SQL)).all()
            answers = {p: resolve(a) for p, a in load_accounts(path, {r.pid for r in rows}).items()}
            decisions = km.plan(rows, answers)
            sdb.execute(text("UPDATE results SET mailing_address = 'PO BOX 9, KENT, WA 98032' "
                             "WHERE id = :i"), {"i": rid})
            sdb.commit()
            return km.apply(sdb, decisions, "2026-09-05")

    counts = await asyncio.to_thread(_race)
    assert counts["skipped_by_write_guard"] == 1
    assert (await db.execute(text("SELECT mailing_address FROM results WHERE id = :i"),
                             {"i": rid})).scalar() == "PO BOX 9, KENT, WA 98032"


@pytest.mark.asyncio
async def test_non_object_enrichment_data_is_skipped_not_replaced(db, business_user, tmp_path):
    rid, _ = await _king_row(db, business_user, mailing="506 S 330TH PL, FEDERAL WAY, WA 98003-5900")
    await db.execute(text("UPDATE results SET enrichment_data = CAST(:v AS json) WHERE id = :i"),
                     {"v": '[null, {"situs_city": "FEDERAL WAY"}]', "i": rid})
    await db.commit()
    path = _extract(tmp_path, [_acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")])

    stats = await asyncio.to_thread(_run, path, True, tmp_path)

    assert stats["writes"]["skipped_by_write_guard"] == 1
    row = (await db.execute(text("SELECT mailing_address, enrichment_data::text AS ed FROM results "
                                 "WHERE id = :i"), {"i": rid})).first()
    assert row.mailing_address == "506 S 330TH PL, FEDERAL WAY, WA 98003-5900"
    assert "situs_city" in row.ed


@pytest.mark.asyncio
async def test_an_unresolvable_echo_is_never_written(db, business_user, tmp_path):
    rid, _ = await _king_row(db, business_user, mailing="506 S 330TH PL, FEDERAL WAY, WA 98003-5900")
    path = _extract(tmp_path, [
        _acct("132140", "0230", "506 S 330TH PL", "FEDERAL WAY WA", "98003"),
        _acct("132140", "0230", "560 NACHES AVE SW #110", "RENTON WA", "98057"),
    ])

    stats = await asyncio.to_thread(_run, path, True, tmp_path)

    assert stats["writes"] == {"left_unchanged_unresolved": 1}
    assert (await db.execute(text("SELECT mailing_address FROM results WHERE id = :i"),
                             {"i": rid})).scalar() == "506 S 330TH PL, FEDERAL WAY, WA 98003-5900"
