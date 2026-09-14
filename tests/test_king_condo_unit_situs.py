"""King condo unit situs + 12-digit recorder account numbers.

King's public GIS parcel layer carries no feature for a condominium UNIT, so ~30% of
King pre-foreclosure leads (45 of 47 blank property addresses in job 85692303,
2026-09-13) could only get a property address from the per-parcel eRealProperty page,
and lost it whenever that source was busy or throttled. The Assessor publishes every
unit's site address in "Condo Complex and Units.zip"; the recorder sometimes prints the
12-digit tax ACCOUNT number where a 10-digit PIN belongs.

Extracts are real zip files built in tmp_path with the county's real column layout and
rows copied from the 2026-09-14 files. DB tests run against the real test database.
"""
from __future__ import annotations

import asyncio
import csv
import io
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.scrapers.enrichment import king_condo_units as kc
from src.scrapers.enrichment import king_rpacct as kr

CONDO_COLUMNS = [
    "Major", "Minor", "UnitType", "BldgNbr", "UnitNbr", "PcntOwnership", "UnitQuality",
    "UnitLoc", "FloorNbr", "TopFloor", "UnitOfMeasure", "Footage", "NbrBedrooms",
    "BathFullCount", "BathHalfCount", "Bath3qtrCount", "Fireplace", "EndUnit", "Condition",
    "OtherRoom", "ViewMountain", "ViewLakeRiver", "ViewCityTerritorial", "ViewPugetSound",
    "ViewLakeWaSamm", "PkgOpen", "PkgCarport", "PkgBasement", "PkgBasementTandem",
    "PkgGarage", "PkgGarageTandem", "PkgOtherType", "Length", "Width", "YrBuilt", "Grade",
    "MHomeDescr", "PersPropAcctNbr", "Address", "BuildingNumber", "Fraction",
    "DirectionPrefix", "StreetName", "StreetType", "DirectionSuffix", "UnitDescr", "ZipCode",
]
RPACCT_COLUMNS = ["AcctNbr", "Major", "Minor", "AttnLine", "AddrLine", "CityState", "ZipCode",
                  "LevyCode", "TaxStat", "BillYr", "NewConstructionFlag", "TaxValReason",
                  "ApprLandVal", "ApprImpsVal", "TaxableLandVal", "TaxableImpsVal"]


def _zip(path: Path, member: str, columns: list[str], rows: list[dict], extra: dict | None = None) -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in columns})
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, buf.getvalue().encode("latin-1"))
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return path


def _condo_zip(tmp_path: Path, rows: list[dict]) -> Path:
    # The real zip also carries the complex file and a readme; the reader must pick the unit CSV.
    return _zip(tmp_path / "condo.zip", "EXTR_CondoUnit2.csv", CONDO_COLUMNS, rows,
                extra={"EXTR_CondoComplex.csv": "Major,ComplexType\r\n026800,1\r\n",
                       "EXTR_CondoUnit2_ReadMe.txt": "readme"})


def _unit(major, minor, address, zipcode, unit_nbr):
    # Address is space-padded in the county file exactly like this.
    return {"Major": major, "Minor": minor, "Address": address, "ZipCode": zipcode,
            "UnitNbr": unit_nbr}


def _rpacct_zip(tmp_path: Path, rows: list[dict]) -> Path:
    return _zip(tmp_path / "rpacct.zip", "EXTR_RPAcct_NoName.csv", RPACCT_COLUMNS, rows)


def _acct(acct, major, minor, addr, city_state, zipcode, tax_stat="T"):
    return {"AcctNbr": acct, "Major": major, "Minor": minor, "AddrLine": addr,
            "CityState": city_state, "ZipCode": zipcode, "TaxStat": tax_stat}


G204 = _unit("026800", "0490", "14527     NE 40TH                      ST      #G204 98007", "98007", "G204")


# ─── unit situs decision rules (pure) ────────────────────────────────────────

class TestUnitSitus:
    def test_trailing_zip_is_split_off_and_the_published_unit_is_kept(self):
        s = kc.unit_situs([G204])
        assert s == kc.UnitSitus("found", street="14527 NE 40TH ST #G204", zip="98007", unit_nbr="G204")

    def test_unit_number_is_never_appended_to_a_street_that_lacks_it(self):
        # "1743 NW 57TH ST" with UnitNbr 403: eRealProperty publishes the same street-only
        # line. UnitNbr is not a postal unit ("E409" is mailed as "#409"), so never append.
        s = kc.unit_situs([_unit("037980", "0165", "1743      NW 57TH                      ST       98107",
                                 "98107", "403")])
        assert s.status == "found" and s.street == "1743 NW 57TH ST" and s.unit_nbr == "403"

    def test_blank_address_is_no_site_address(self):
        assert kc.unit_situs([_unit("326835", "0090", "", "", "8")]).status == "no_site_address"

    def test_a_different_trailing_zip_is_a_conflict_not_a_guess(self):
        s = kc.unit_situs([_unit("100000", "0001", "100 MAIN ST 98032", "98002", "1")])
        assert s.status == "zip_conflict" and s.street is None

    def test_no_zipcode_keeps_the_street_without_a_zip(self):
        s = kc.unit_situs([_unit("549090", "0040", "148 107TH AVE SE #4", "", "4")])
        assert s == kc.UnitSitus("found", street="148 107TH AVE SE #4", zip=None, unit_nbr="4")

    def test_absent(self):
        assert kc.unit_situs(None).status == "absent"
        assert kc.unit_situs([]).status == "absent"

    def test_two_rows_naming_different_addresses_are_ambiguous(self):
        a = _unit("100000", "0001", "1 A ST 98001", "98001", "1")
        b = _unit("100000", "0001", "2 B ST 98001", "98001", "1")
        assert kc.unit_situs([a, b]).status == "ambiguous"


class TestComposeWithComplexLocality:
    def test_complex_zip_agreeing_with_unit_zip_gives_the_full_gis_shape(self):
        situs = kc.UnitSitus("found", street="14527 NE 40TH ST #G204", zip="98007", unit_nbr="G204")
        fill = kc.compose_fill(situs, {"situs_city": "BELLEVUE", "situs_state": "WA", "situs_zip": "98007"})
        assert fill == kc.Fill(property_address="14527 NE 40TH ST #G204, BELLEVUE, WA 98007",
                               city="BELLEVUE", state="WA", zip="98007")

    def test_complex_zip_disagreeing_is_not_used(self):
        # 3110700100: unit 1727 HARBOR AVE SW 98116, complex 3110700000 ZIP5 98126 (live).
        situs = kc.UnitSitus("found", street="1727 HARBOR AVE SW", zip="98116", unit_nbr="206")
        assert kc.compose_fill(situs, {"situs_city": "SEATTLE", "situs_state": "WA",
                                       "situs_zip": "98126"}) is None

    def test_no_complex_no_city_no_state_or_no_unit_zip_is_no_fill(self):
        situs = kc.UnitSitus("found", street="1 A ST", zip="98001", unit_nbr="1")
        assert kc.compose_fill(situs, None) is None
        assert kc.compose_fill(situs, {"situs_city": None, "situs_state": "WA", "situs_zip": "98001"}) is None
        assert kc.compose_fill(situs, {"situs_city": "KENT", "situs_state": "Washington",
                                       "situs_zip": "98001"}) is None
        no_zip = kc.UnitSitus("found", street="1 A ST", zip=None, unit_nbr="1")
        assert kc.compose_fill(no_zip, {"situs_city": "KENT", "situs_state": "WA", "situs_zip": "98001"}) is None

    def test_complex_pin(self):
        assert kc.complex_pin("0268000490") == "0268000000"


class TestCondoReader:
    def test_loads_only_requested_pins_with_leading_zeros(self, tmp_path):
        path = _condo_zip(tmp_path, [G204, _unit("268000", "0490", "9 OTHER ST 98001", "98001", "1")])
        got = kc.load_units(path, {"0268000490"})
        assert list(got) == ["0268000490"] and len(got["0268000490"]) == 1

    def test_a_changed_schema_fails_loudly(self, tmp_path):
        path = _zip(tmp_path / "bad.zip", "EXTR_CondoUnit2.csv", ["Major", "Minor"], [])
        with pytest.raises(RuntimeError, match="schema"):
            kc.load_units(path, {"0268000490"})

    def test_resolve_units_without_an_extract_is_none(self, monkeypatch):
        monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: None)
        assert kc.resolve_units({"0268000490"}) is None

    def test_a_failed_refresh_keeps_the_old_file_and_a_time_limit_is_reraised(self, monkeypatch, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "condo.zip").write_bytes(_condo_zip(tmp_path, [G204]).read_bytes())
        (cache / "snapshot.json").write_text('{"snapshot": "2026-09-12"}', encoding="utf-8")
        old = __import__("time").time() - 2 * 24 * 3600
        __import__("os").utime(cache / "condo.zip", (old, old))
        monkeypatch.setattr(kc, "_CACHE_DIR", cache)

        def _fail(*a, **kw):
            raise RuntimeError("county site down")

        monkeypatch.setattr(kc, "download_extract", _fail)
        assert _REAL_CONDO_CACHED() == (cache / "condo.zip", "2026-09-12")

        class SoftTimeLimitExceeded(Exception):
            pass

        def _limit(*a, **kw):
            raise SoftTimeLimitExceeded()

        monkeypatch.setattr(kc, "download_extract", _limit)
        with pytest.raises(SoftTimeLimitExceeded):
            _REAL_CONDO_CACHED()
        assert not list(cache.glob("*.part"))


_REAL_CONDO_CACHED = kc.cached_extract  # captured at import, before conftest patches it


# ─── 12-digit recorder account numbers (pure over a real zip) ───────────────

class TestAccountNumbers:
    def test_an_exact_account_resolves_to_its_pin(self, tmp_path):
        path = _rpacct_zip(tmp_path, [
            _acct("012603938700", "012603", "9387", "302 NW 203RD ST", "SHORELINE  WA", "98177"),
            _acct("192106911402", "192106", "9114", "33713 186TH AVE SE", "AUBURN  WA", "98092"),
        ])
        assert kr.load_account_pins(path, {"012603938700", "192106911402", "999999999999"}) == {
            "012603938700": "0126039387", "192106911402": "1921069114"}

    def test_a_split_taxable_exempt_account_is_still_one_pin(self, tmp_path):
        path = _rpacct_zip(tmp_path, [
            _acct("000020004305", "000020", "0043", "220 4TH AVE S", "SEATTLE WA", "98104", "T"),
            _acct("000020004305", "000020", "0043", "220 4TH AVE S", "SEATTLE WA", "98104", "X"),
        ])
        assert kr.load_account_pins(path, {"000020004305"}) == {"000020004305": "0000200043"}

    def test_an_account_naming_two_pins_or_disagreeing_with_its_pin_fails_closed(self, tmp_path):
        path = _rpacct_zip(tmp_path, [
            _acct("111111222201", "111111", "2222", "1 A ST", "KENT WA", "98032"),
            _acct("111111222201", "111111", "2223", "1 A ST", "KENT WA", "98032"),
            _acct("333333444401", "555555", "4444", "1 B ST", "KENT WA", "98032"),
            _acct("12603938700", "012603", "9387", "short acct", "KENT WA", "98032"),
            _acct("777777888801", "77777", "8888", "short major", "KENT WA", "98032"),
        ])
        assert kr.load_account_pins(path, {"111111222201", "333333444401", "12603938700",
                                           "777777888801"}) == {}


# ─── the live King enrichment pass ───────────────────────────────────────────

async def _prefc_job(db, user):
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King prefc condo",
                           county="king", state="WA", record_type="pre_foreclosure",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="enriching",
               trigger="manual", record_count=4, billed_count=4))
    await db.commit()
    return job_id


def _row(db, user, job_id, parcel, *, prop=None, mailing=None, source="king_landmark_json", party="DOE JANE"):
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=parcel,
                  property_address=prop, mailing_address=mailing, skip_trace_status="not_attempted",
                  is_duplicate=False, dedup_hash=f"hash-{parcel}", source_fingerprint=f"fp-{parcel}",
                  doc_type="NOTICE OF TRUSTEE SALE",
                  enrichment_data={"source": source, "instrument_number": "20260709000383"}))
    return rid


def _gis_from(table: dict):
    asked: list[list[str]] = []

    def _gis(parcel_ids, county, state, stats=None):
        asked.append(list(parcel_ids))
        return {p: dict(table[p]) for p in parcel_ids if p in table}

    return _gis, asked


def _gis_row(street, city, zipcode):
    return {"property_address": f"{street}, {city}, WA {zipcode}", "mailing_address": None,
            "matched": True, "vacant_no_situs": False, "situs_city": city, "situs_state": "WA",
            "situs_zip": zipcode, "property_city": city, "property_state": "WA", "property_zip": zipcode}


def _run_enrichment(redis_client, job_id):
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.enrich import _run_inline_enrichment

    with system_sync_session() as sdb:
        job = sdb.get(Job, str(job_id))
        config = sdb.get(ScraperConfig, job.scraper_config_id)
        _run_inline_enrichment(sdb, job, redis_client, str(job_id), config, summary={})


async def _fetch(db, rid):
    return (await db.execute(text(
        "SELECT parcel_id, property_address, property_city, property_state, property_zip, "
        "mailing_address, enrichment_data, dedup_hash, source_fingerprint FROM results WHERE id = :i"),
        {"i": rid})).first()


@pytest.mark.asyncio
async def test_king_job_fills_condo_units_and_account_number_parcels(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    condo = _row(db, business_user, job_id, "0268000490", mailing="14527 NE 40TH ST #G204, BELLEVUE, WA 98007")
    acct = _row(db, business_user, job_id, "012603938700", party="RONSTAD ERIC R")
    zip_conflict = _row(db, business_user, job_id, "3110700100", mailing="1 X ST, SEATTLE, WA 98126")
    blank = _row(db, business_user, job_id, "3268350090", mailing="P O BOX 46663, SEATTLE, WA 98106")
    already = _row(db, business_user, job_id, "0013001905", prop="8822 2ND AVE S, SEATTLE, WA 98108",
                   mailing="8822 2ND AVE S, SEATTLE, WA 98108")
    other_source = _row(db, business_user, job_id, "192106911402", source="king_socrata")
    await db.commit()

    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [
        G204,
        _unit("311070", "0100", "1727         HARBOR                    AVE  SW  98116", "98116", "206"),
        _unit("326835", "0090", "", "", "8"),
    ]), "2026-09-14"))
    rp = _rpacct_zip(tmp_path, [
        _acct("012603938700", "012603", "9387", "302 NW 203RD ST", "SHORELINE  WA", "98177"),
        _acct("192106911402", "192106", "9114", "33713 186TH AVE SE", "AUBURN  WA", "98092"),
    ])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (rp, "2026-09-05"))
    gis, gis_asked = _gis_from({
        "0126039387": _gis_row("302 NW 203RD ST", "SHORELINE", "98177"),
        "0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007"),
        "3110700000": _gis_row("1727 HARBOR AVE SW", "SEATTLE", "98126"),
        "3268350000": _gis_row("9025 15TH AVE SW", "SEATTLE", "98106"),
    })
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)
    erp_asked: list = []

    async def _erp(parcel_ids, **kw):
        erp_asked.extend(parcel_ids)
        return {}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    await asyncio.to_thread(_run_enrichment, redis_client, job_id)

    # Condo unit: full address from the unit extract + corroborated complex locality.
    r = await _fetch(db, condo)
    assert r.property_address == "14527 NE 40TH ST #G204, BELLEVUE, WA 98007"
    assert (r.property_city, r.property_state, r.property_zip) == ("BELLEVUE", "WA", "98007")
    assert r.enrichment_data["property_source"] == "king_condo_unit"
    assert r.enrichment_data["property_source_snapshot"] == "2026-09-14"
    assert r.enrichment_data["condo_unit_nbr"] == "G204"
    assert r.enrichment_data["property_locality_source"] == "king_gis_complex:0268000000"
    assert r.enrichment_data["instrument_number"] == "20260709000383"   # existing keys survive

    # 12-digit account number: parcel_id and billing identity untouched, lookups use the PIN.
    r = await _fetch(db, acct)
    assert r.parcel_id == "012603938700"
    assert (r.dedup_hash, r.source_fingerprint) == ("hash-012603938700", "fp-012603938700")
    assert r.enrichment_data["resolved_parcel_id"] == "0126039387"
    assert r.enrichment_data["source_parcel_id"] == "012603938700"
    assert r.enrichment_data["resolved_by"] == "rpacct_account_number"
    assert r.enrichment_data["resolved_snapshot"] == "2026-09-05"
    assert r.property_address == "302 NW 203RD ST, SHORELINE, WA 98177"
    assert r.mailing_address == "302 NW 203RD ST, SHORELINE, WA 98177"
    assert any("0126039387" in batch for batch in gis_asked)
    assert not any("012603938700" in batch for batch in gis_asked)

    # Unit ZIP disagrees with the complex: no address invented, reason kept with its snapshot.
    r = await _fetch(db, zip_conflict)
    assert r.property_address is None and r.property_city is None
    assert r.enrichment_data["condo_unit_status"] == "no_locality"
    assert r.enrichment_data["condo_unit_snapshot"] == "2026-09-14"

    # Blank unit address in the extract: left NULL, marked, eRealProperty still allowed later.
    r = await _fetch(db, blank)
    assert r.property_address is None
    assert r.enrichment_data["condo_unit_status"] == "no_site_address"

    # A value already present is never touched.
    r = await _fetch(db, already)
    assert r.property_address == "8822 2ND AVE S, SEATTLE, WA 98108"
    assert "property_source" not in r.enrichment_data

    # A 12-digit value from a non-recorder source is not treated as an account number.
    r = await _fetch(db, other_source)
    assert "resolved_parcel_id" not in r.enrichment_data

    # No quota, no skip trace from enrichment.
    job = (await db.execute(text("SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"),
                            {"j": job_id})).first()
    assert (job.billed_count, job.billing_applied_at) == (4, None)
    assert (await db.execute(text("SELECT count(*) FROM pending_skip_trace_rows WHERE job_id = :j"),
                             {"j": job_id})).scalar() == 0


@pytest.mark.asyncio
async def test_a_property_address_written_concurrently_is_not_overwritten(
    db, business_user, tmp_path, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "0268000490")
    await db.commit()
    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [G204]), "2026-09-14"))
    gis, _ = _gis_from({"0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007")})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _fill_king_condo_unit_situs

        with system_sync_session() as sdb:
            res = sdb.get(Result, rid)
            _ = res.property_address  # loaded empty
            with system_sync_session() as other:
                other.execute(text("UPDATE results SET property_address = 'FROM ERP 98007' WHERE id = :i"),
                              {"i": rid})
                other.commit()
            filled = _fill_king_condo_unit_situs(sdb, [res], "job")
            sdb.commit()
            return filled, res.property_address

    filled, in_session = await asyncio.to_thread(_go)
    assert filled == 0
    assert (await _fetch(db, rid)).property_address == "FROM ERP 98007"
    # The job's own object was reloaded, so no later pass can write its stale empty copy
    # (and then a page value) back over the concurrent one.
    assert in_session == "FROM ERP 98007"


@pytest.mark.asyncio
async def test_a_none_in_a_marker_never_blanks_a_stored_key(db, business_user, tmp_path, monkeypatch):
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "3268350090")
    await db.commit()
    await db.execute(text("UPDATE results SET enrichment_data = CAST(:e AS json) WHERE id = :i"),
                     {"e": '{"source": "king_landmark_json", "condo_unit_nbr": "8"}', "i": rid})
    await db.commit()
    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [
        _unit("326835", "0090", "", "", "")]), "2026-09-14"))

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _fill_king_condo_unit_situs

        with system_sync_session() as sdb:
            _fill_king_condo_unit_situs(sdb, [sdb.get(Result, rid)], "job")
            sdb.commit()

    await asyncio.to_thread(_go)
    ed = (await _fetch(db, rid)).enrichment_data
    assert ed["condo_unit_status"] == "no_site_address"
    assert ed["condo_unit_nbr"] == "8"


@pytest.mark.asyncio
async def test_erealproperty_cannot_overwrite_an_account_number_resolution(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "012603938700", party="RONSTAD ERIC R")
    await db.commit()
    rp = _rpacct_zip(tmp_path, [_acct("012603938700", "012603", "9387", "", "", "")])  # no mailing
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (rp, "2026-09-05"))
    gis, _ = _gis_from({})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)

    async def _erp(parcel_ids, **kw):
        return {p: {"parcel_lookup": "recovered", "resolved_parcel_id": "2603938700",
                    "source_parcel_id": p, "resolved_by": "gis_singleton"} for p in parcel_ids}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    await asyncio.to_thread(_run_enrichment, redis_client, job_id)

    ed = (await _fetch(db, rid)).enrichment_data
    assert ed["resolved_parcel_id"] == "0126039387"
    assert ed["resolved_by"] == "rpacct_account_number"
    assert ed["resolved_conflict"] == {"resolved_parcel_id": "2603938700", "resolved_by": "gis_singleton"}


@pytest.mark.asyncio
async def test_a_page_that_does_not_prove_the_resolved_pin_writes_nothing(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "012603938700", party="RONSTAD ERIC R")
    await db.commit()
    rp = _rpacct_zip(tmp_path, [_acct("012603938700", "012603", "9387", "", "", "")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (rp, "2026-09-05"))
    gis, _ = _gis_from({})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)

    async def _erp(parcel_ids, **kw):
        # Any status, no resolved PIN: the page is not proven to be 0126039387.
        return {p: {"property_address": "9 WRONG ST 98001", "mailing_address": "9 WRONG ST, KENT, WA 98001",
                    "owner_name": "SOMEONE ELSE", "parcel_lookup": "verified"} for p in parcel_ids}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    await asyncio.to_thread(_run_enrichment, redis_client, job_id)

    r = await _fetch(db, rid)
    assert r.property_address is None and r.mailing_address is None
    assert r.enrichment_data["resolved_parcel_id"] == "0126039387"
    assert "resolved_conflict" not in r.enrichment_data


@pytest.mark.asyncio
async def test_the_page_fills_a_placeholder_property_address(
    db, business_user, redis_client, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "2388800070", prop="(enrichment unavailable)")
    await db.commit()
    gis, _ = _gis_from({})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)

    async def _erp(parcel_ids, **kw):
        return {p: {"property_address": "49 W ETRURIA ST #401 98119", "mailing_address": None,
                    "owner_name": "BAE PAIGE MOONJONG", "parcel_lookup": "verified"} for p in parcel_ids}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    await asyncio.to_thread(_run_enrichment, redis_client, job_id)

    r = await _fetch(db, rid)
    assert r.property_address == "49 W ETRURIA ST #401 98119"
    assert r.property_zip == "98119"


@pytest.mark.asyncio
async def test_a_mailing_page_seeded_with_the_same_pin_is_applied(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    """Phase 2 returns phase 1's own row (results_seed) with the mailing added, so a page
    proven to be the resolved PIN carries it and must not be blocked by the guard."""
    job_id = await _prefc_job(db, business_user)
    rid = _row(db, business_user, job_id, "012603938700", party="RONSTAD ERIC R")
    await db.commit()
    rp = _rpacct_zip(tmp_path, [_acct("012603938700", "012603", "9387", "", "", "")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (rp, "2026-09-05"))
    gis, _ = _gis_from({})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)
    seeds_seen: list = []

    async def _erp(parcel_ids, **kw):
        if kw.get("tax_urls_out") is not None:  # pass 1: recovered to OUR pin
            out = {}
            for p in parcel_ids:
                kw["tax_urls_out"][p] = f"https://payment.kingcounty.gov/x?p={p}"
                out[p] = {"property_address": None, "mailing_address": None, "owner_name": None,
                          "parcel_lookup": "recovered", "resolved_parcel_id": "0126039387",
                          "source_parcel_id": p, "resolved_by": "gis_singleton"}
            return out
        seeds_seen.append(kw.get("results_seed"))  # pass 2: the seeded row + mailing
        return {p: {**dict((kw.get("results_seed") or {}).get(p) or {}),
                    "mailing_address": "302 NW 203RD ST, SHORELINE, WA 98177"}
                for p in (kw.get("tax_urls_in") or {})}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    await asyncio.to_thread(_run_enrichment, redis_client, job_id)

    assert seeds_seen and seeds_seen[0]["012603938700"]["resolved_parcel_id"] == "0126039387"
    r = await _fetch(db, rid)
    assert r.mailing_address == "302 NW 203RD ST, SHORELINE, WA 98177"
    assert r.enrichment_data["resolved_by"] == "rpacct_account_number"
    assert "resolved_conflict" not in r.enrichment_data


@pytest.mark.asyncio
async def test_a_row_deleted_mid_sweep_does_not_undo_the_other_fills(
    db, business_user, tmp_path, monkeypatch,
):
    job_id = await _prefc_job(db, business_user)
    gone = _row(db, business_user, job_id, "0125000060")
    kept = _row(db, business_user, job_id, "0268000490")
    await db.commit()
    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [
        G204, _unit("012500", "0060", "3028         WESTERN                   AVE     #106 98121",
                    "98121", "106")]), "2026-09-14"))
    gis, _ = _gis_from({"0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007"),
                        "0125000000": _gis_row("3028 WESTERN AVE", "SEATTLE", "98121")})
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", gis)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _fill_king_condo_unit_situs

        with system_sync_session() as sdb:
            rows = [sdb.get(Result, gone), sdb.get(Result, kept)]
            _ = [r.property_address for r in rows]
            with system_sync_session() as other:
                other.execute(text("DELETE FROM results WHERE id = :i"), {"i": gone})
                other.commit()
            filled = _fill_king_condo_unit_situs(sdb, rows, "job")
            sdb.commit()
            return filled

    assert await asyncio.to_thread(_go) == 1
    assert (await _fetch(db, kept)).property_address == "14527 NE 40TH ST #G204, BELLEVUE, WA 98007"


def test_job_mailing_prefill_uses_the_resolved_pin(monkeypatch, tmp_path):
    from src.workers.tasks_helpers.enrich import _fill_king_mailing_from_extract

    rp = _rpacct_zip(tmp_path, [
        _acct("012603938700", "012603", "9387", "302 NW 203RD ST", "SHORELINE  WA", "98177")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (rp, "2026-09-05"))
    row = SimpleNamespace(parcel_id="012603938700", mailing_address=None, enrichment_data={
        "resolved_parcel_id": "0126039387", "resolved_by": "rpacct_account_number"})

    assert _fill_king_mailing_from_extract({"012603938700": [row]}, "job") == 1
    assert row.mailing_address == "302 NW 203RD ST, SHORELINE, WA 98177"
