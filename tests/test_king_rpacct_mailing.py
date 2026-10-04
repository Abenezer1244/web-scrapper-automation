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

    def test_any_recorded_provenance_is_never_second_guessed(self):
        for kw in ({"mailing_source": "king_assessor_tax_bill"}, {"mailing_source": "Some_Other"},
                   {"recovery_outcome": "found"}, {"recovery_outcome": "none"}):
            assert km.plan([_row(**kw)], {"1321400230": TX}) == []

    def test_a_blank_locality_is_no_address(self):
        assert kr.format_mailing({"AddrLine": "100 MAIN ST", "CityState": "  ",
                                  "ZipCode": "98032"}) is None

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


# ─── Phase B: live enrichment + recovery consult the extract first ───────────

class TestCachedExtract:
    def test_a_fresh_file_is_reused_without_downloading(self, monkeypatch, tmp_path):
        from src.scrapers.enrichment import king_rpacct as mod

        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "rpacct.zip").write_bytes(_extract(tmp_path, []).read_bytes())
        (cache / "snapshot.json").write_text('{"snapshot": "2026-09-05"}', encoding="utf-8")
        monkeypatch.setattr(mod, "_CACHE_DIR", cache)
        monkeypatch.setattr(mod, "download_extract",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("downloaded")))
        path, snap = _REAL_CACHED_EXTRACT()
        assert path == cache / "rpacct.zip" and snap == "2026-09-05"

    def test_a_failed_refresh_keeps_the_old_file(self, monkeypatch, tmp_path):
        import os
        import time

        from src.scrapers.enrichment import king_rpacct as mod

        cache = tmp_path / "cache"
        cache.mkdir()
        z = cache / "rpacct.zip"
        z.write_bytes(_extract(tmp_path, []).read_bytes())
        old = time.time() - 3 * 24 * 3600  # stale but inside the 14-day limit
        os.utime(z, (old, old))
        monkeypatch.setattr(mod, "_CACHE_DIR", cache)

        def _fail(*a, **kw):
            raise RuntimeError("county site down")

        monkeypatch.setattr(mod, "download_extract", _fail)
        assert _REAL_CACHED_EXTRACT()[0] == z

    def test_no_file_and_no_download_means_fall_back_to_pages(self, monkeypatch, tmp_path):
        from src.scrapers.enrichment import king_rpacct as mod

        monkeypatch.setattr(mod, "_CACHE_DIR", tmp_path / "empty")

        def _fail(*a, **kw):
            raise RuntimeError("county site down")

        monkeypatch.setattr(mod, "download_extract", _fail)
        assert _REAL_CACHED_EXTRACT() is None


_REAL_CACHED_EXTRACT = kr.cached_extract  # captured at import, before conftest patches it


def _use_extract(monkeypatch, path):
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (path, "2026-09-05"))


def test_job_prefill_fills_only_missing_rows_from_an_unambiguous_answer(monkeypatch, tmp_path):
    from src.workers.tasks_helpers.enrich import _fill_king_mailing_from_extract

    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051"),
        _acct("145360", "1063", "12541 A 35TH AVE NE", "SEATTLE WA", "98125"),
        _acct("145360", "1063", "560 NACHES AVE SW #110", "RENTON WA", "98057"),
    ]))
    empty = SimpleNamespace(mailing_address=None, enrichment_data={"keep": 1})
    already = SimpleNamespace(mailing_address="PO BOX 1, KENT, WA 98032", enrichment_data=None)
    ambiguous = SimpleNamespace(mailing_address=None, enrichment_data=None)

    filled = _fill_king_mailing_from_extract(
        {"1321400230": [empty, already], "1453601063": [ambiguous]}, "job")

    assert filled == 1
    assert empty.mailing_address == "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051"
    assert empty.enrichment_data == {"keep": 1, "mailing_source": "king_rpacct",
                                     "mailing_rpacct_snapshot": "2026-09-05"}
    assert already.mailing_address == "PO BOX 1, KENT, WA 98032"
    assert ambiguous.mailing_address is None


def test_job_prefill_without_an_extract_changes_nothing(monkeypatch):
    from src.workers.tasks_helpers.enrich import _fill_king_mailing_from_extract

    row = SimpleNamespace(mailing_address=None, enrichment_data=None)
    assert _fill_king_mailing_from_extract({"1321400230": [row]}, "job") == 0
    assert row.mailing_address is None


@pytest.mark.asyncio
async def test_recovery_takes_the_extract_answer_and_never_asks_the_page(
    db, business_user, tmp_path, monkeypatch,
):
    from src.workers import mailing_recovery as mr

    rid, _ = await _king_row(db, business_user, mailing=None, deferred=True)
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")]))
    monkeypatch.setattr("src.scrapers.enrichment.source_health.is_source_available",
                        lambda *a, **kw: True)
    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: False)
    monkeypatch.setattr(mr, "_release_single_flight", lambda _c: None)
    asked: list = []

    async def _page(parcels, **kw):
        asked.extend(parcels)
        return {}

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _page)

    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert asked == [] and stats["extract_found"] == 1
    row = (await db.execute(text("SELECT mailing_address, enrichment_data FROM results "
                                 "WHERE id = :i"), {"i": rid})).first()
    assert row.mailing_address == "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051"
    assert row.enrichment_data["mailing_source"] == "king_rpacct"
    assert row.enrichment_data["mailing_recovery_outcome"] == "found"
    assert row.enrichment_data["mailing_rpacct_snapshot"] == "2026-09-05"


@pytest.mark.asyncio
async def test_a_live_king_job_skips_the_tax_bill_page_for_extract_answered_parcels(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    """Behavioral, not source-shape: run the real enrichment pass for a King job."""
    answered, _ = await _king_row(db, business_user, mailing=None, status="enriching",
                                  parcel="1321400230")
    job_id = (await db.execute(text("SELECT job_id FROM results WHERE id = :i"),
                               {"i": answered})).scalar()
    other = str(uuid.uuid4())
    db.add(Result(id=other, user_id=business_user.id, job_id=job_id, party_name="DOE JANE",
                  parcel_id="9999900001", property_address="1 MAIN ST", mailing_address=None,
                  skip_trace_status="not_attempted", is_duplicate=False))
    await db.commit()
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("132140", "0230", "2736 ROSECLIFF TERRACE", "GRAPEVINE TX", "76051")]))
    page_mailing_asked: list = []

    async def _king(parcel_ids, **kw):
        if kw.get("tax_urls_out") is not None:          # pass 1: property + tax-bill URL
            for p in parcel_ids:
                kw["tax_urls_out"][p] = f"https://payment.kingcounty.gov/x?p={p}"
            return {p: {"property_address": None} for p in parcel_ids}
        page_mailing_asked.extend(kw.get("tax_urls_in") or {})  # pass 2: mailing page
        return {}

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _king)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, str(job_id))
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            _run_inline_enrichment(sdb, job, redis_client, str(job_id), config, summary={})

    await asyncio.to_thread(_go)

    assert page_mailing_asked == ["9999900001"]
    assert (await db.execute(text("SELECT mailing_address FROM results WHERE id = :i"),
                             {"i": answered})).scalar() == "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051"



def test_a_file_older_than_the_stale_limit_is_not_used(monkeypatch, tmp_path):
    import os
    import time

    from src.scrapers.enrichment import king_rpacct as mod

    cache = tmp_path / "cache"
    cache.mkdir()
    z = cache / "rpacct.zip"
    z.write_bytes(_extract(tmp_path, []).read_bytes())
    old = time.time() - 30 * 24 * 3600
    os.utime(z, (old, old))
    monkeypatch.setattr(mod, "_CACHE_DIR", cache)

    def _fail(*a, **kw):
        raise RuntimeError("county site down")

    monkeypatch.setattr(mod, "download_extract", _fail)
    assert _REAL_CACHED_EXTRACT() is None
    assert not list(cache.glob("*.part"))


def test_a_soft_time_limit_is_never_swallowed(monkeypatch, tmp_path):
    from src.scrapers.enrichment import king_rpacct as mod

    class SoftTimeLimitExceeded(Exception):
        pass

    monkeypatch.setattr(mod, "_CACHE_DIR", tmp_path / "c")

    def _limit(*a, **kw):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(mod, "download_extract", _limit)
    with pytest.raises(SoftTimeLimitExceeded):
        _REAL_CACHED_EXTRACT()


# ─── download size (2026-10-04) ──────────────────────────────────────────────

class TestExtractDownloadSize:
    """The RPAcct extract is ~18 MB, past safe_get's 16 MiB page default; every refresh
    failed for two weeks. Real HTTP, real streaming cap; only the URL validator is
    bypassed (the SSRF guard has its own suite, and CI must not depend on DNS)."""

    @staticmethod
    def _plain_transport(monkeypatch):
        """Stand in for the SSRF layers (URL validator + pinned transport), which refuse
        loopback by design and have their own suite in test_safe_http.py. The streaming
        size cap under test (_read_capped) stays real."""
        import requests

        from src.utils import safe_http

        monkeypatch.setattr(safe_http, "validate_scraping_target", lambda *a, **kw: None)
        monkeypatch.setattr(safe_http, "_get", lambda url, **kw: requests.get(url, **kw))

    def _serve(self, body: bytes, declared: int | None = None):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(declared or len(body)))
                self.send_header("Last-Modified", "Sat, 26 Sep 2026 00:33:20 GMT")
                self.end_headers()
                if declared is None:
                    self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}/Real%20Property%20Account.zip"

    def test_an_extract_larger_than_the_page_default_downloads(self, monkeypatch, tmp_path):
        import os

        src = _extract(tmp_path, [_acct("090400", "0025", "PO BOX 5003", "BELLEVUE WA", "98009")])
        with zipfile.ZipFile(src, "a") as zf:      # pad past 16 MiB, incompressible
            zf.writestr(zipfile.ZipInfo("padding.bin"), os.urandom(17 * 1024 * 1024))
        body = src.read_bytes()
        assert len(body) > 16 * 1024 * 1024
        server, url = self._serve(body)
        self._plain_transport(monkeypatch)
        try:
            dest = tmp_path / "out.zip"
            assert kr.download_zip(url, dest, kr._csv_name, "King RPAcct") == "2026-09-26"
            assert dest.read_bytes() == body
        finally:
            server.shutdown()

    def test_the_cap_is_still_a_hard_bound(self, monkeypatch, tmp_path):
        server, url = self._serve(b"", declared=kr._EXTRACT_MAX_BYTES + 1)
        self._plain_transport(monkeypatch)
        try:
            with pytest.raises(Exception, match="too large"):
                kr.download_zip(url, tmp_path / "out.zip", kr._csv_name, "King RPAcct")
        finally:
            server.shutdown()
