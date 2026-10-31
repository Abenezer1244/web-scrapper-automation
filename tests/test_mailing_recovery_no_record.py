"""Mailing recovery settles a King PIN only when three King sources agree it does not exist.

Seen live 2026-10-31: the same 4 parcels (10 leads) were fetched every 10 minutes,
forever. Each page was eRealProperty's "No data found." answer, which phase 1 counts as
a failure; failures are never charged, and the breaker meant to stop a persistent
failure needs 30 of them, which a 4-parcel backlog never reaches.

Real DB, real rows, a real Assessor extract zip in a real worker cache (tmp_path; only the
download is substituted), and the real King GIS code with only its HTTP request
substituted. Most cases substitute the eRealProperty pass as test_mailing_recovery does,
because what is under test is what the sweep WRITES; one runs the real pass end to end.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_rpacct as kr
from src.workers import mailing_recovery as mr
from tests.test_king_no_record_page import NO_RECORD_PAGE, _Resp, offline  # noqa: F401  (fixture)

PIN = "9999990001"
SNAPSHOT = "2026-10-31"


@pytest.fixture(autouse=True)
def _healthy_no_lock(monkeypatch):
    monkeypatch.setattr("src.scrapers.enrichment.source_health.is_source_available", lambda *_a, **_k: True)
    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: False)
    monkeypatch.setattr(mr, "_release_single_flight", lambda _c: None)


async def _row(db, user, parcel=PIN) -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King prefc", county="king", state="WA",
                           record_type="pre_foreclosure", fields=["party_name"], enrichment=[],
                           schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="done", trigger="manual",
               record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE", parcel_id=parcel,
                  property_address=None, mailing_address=None, skip_trace_status="not_attempted",
                  is_duplicate=False, enrichment_data={"mailing_lookup_deferred": True}))
    await db.commit()
    return rid


_REAL_CACHED_EXTRACT = kr.cached_extract  # captured at import, before conftest stubs it


def _real_cache(monkeypatch, cache_dir) -> list:
    """The real worker cache, rooted in `cache_dir`. Returns the download attempts made."""
    downloads: list = []

    def _download(dest):
        downloads.append(dest)
        raise OSError("no network in tests")

    monkeypatch.setattr(kr, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(kr, "download_extract", _download)
    monkeypatch.setattr(kr, "cached_extract", _REAL_CACHED_EXTRACT)
    return downloads


def _use_extract(monkeypatch, tmp_path, accounts: list[dict]) -> list:
    """A real RPAcct zip in a fresh real cache, read by the real reader. `accounts` never
    names PIN unless asked. Returns the download attempts (a fresh cache makes none)."""
    cols = ["AcctNbr", "Major", "Minor", "AttnLine", "AddrLine", "CityState", "ZipCode"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for a in accounts:
        writer.writerow({c: a.get(c, "") for c in cols})
    cache = tmp_path / "rpacct_cache"
    cache.mkdir()
    with zipfile.ZipFile(cache / "rpacct.zip", "w") as zf:
        zf.writestr("EXTR_RPAcct_NoName.csv", buf.getvalue().encode("latin-1"))
    (cache / "snapshot.json").write_text(json.dumps({"snapshot": SNAPSHOT}), encoding="utf-8")
    return _real_cache(monkeypatch, cache)


_OTHER_ACCOUNT = {"Major": "132140", "Minor": "0230", "AddrLine": "2736 ROSECLIFF TERRACE",
                  "CityState": "GRAPEVINE TX", "ZipCode": "76051"}


def _page(monkeypatch, *, label: str | None = "no_record") -> list:
    """eRealProperty answered phase 1 for every parcel; no mailing request was made."""
    asked: list = []

    async def _batch(parcels, **kw):
        asked.append(list(parcels))
        kw["stats"].update({"requested_pids": list(parcels), "mailing_attempted_pids": []})
        if label is None:
            return {}
        return {p: {"property_address": None, "mailing_address": None, "owner_name": None,
                    "parcel_lookup": label} for p in parcels}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _batch)
    return asked


class _GisResp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _gis(monkeypatch, *, status: int = 200, body: dict | None = None, raises: bool = False) -> list:
    """King's parcel layer, at the HTTP boundary only. Default: it carries no such PIN."""
    calls: list = []

    def _get(url, params=None, **kw):
        calls.append(params["where"])
        if raises:
            raise OSError("connection reset")
        return _GisResp(status, {"features": []} if body is None else body)

    monkeypatch.setattr("src.scrapers.enrichment.county_gis.safe_get", _get)
    return calls


async def _ed(db, rid) -> dict:
    return (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()


def _rotated_uncharged(ed: dict) -> bool:
    return (ed["mailing_lookup_deferred"] is True and "mailing_recovery_attempts" not in ed
            and "mailing_recovery_outcome" not in ed and bool(ed.get("mailing_recovery_last_at")))


@pytest.mark.asyncio
async def test_three_agreeing_sources_settle_the_pin_and_it_is_never_asked_again(
        db, business_user, monkeypatch, tmp_path):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    asked = _page(monkeypatch)
    gis = _gis(monkeypatch)

    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert stats["none"] == 1 and len(gis) == 1 and PIN in gis[0]
    ed = await _ed(db, rid)
    assert ed["mailing_recovery_outcome"] == "parcel_not_found"
    assert ed["mailing_lookup_deferred"] is False and ed["mailing_recovery_attempts"] == 1
    assert ed["mailing_source"] == kca.NOT_CARRIED_SOURCE
    assert ed["mailing_rpacct_snapshot"] == SNAPSHOT
    mailing = (await db.execute(text("SELECT mailing_address FROM results WHERE id = :i"), {"i": rid})).scalar()
    assert mailing is None

    await asyncio.to_thread(mr.recover_deferred_king_mailing)
    assert asked == [[PIN]]                                    # settled: never fetched again


@pytest.mark.asyncio
async def test_without_a_usable_extract_it_keeps_rotating(db, business_user, monkeypatch):
    rid = await _row(db, business_user)                        # conftest: no extract at all
    _page(monkeypatch)
    gis = _gis(monkeypatch)

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert gis == [] and _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
async def test_an_extract_that_lists_the_pin_blocks_it(db, business_user, monkeypatch, tmp_path):
    rid = await _row(db, business_user)
    # An account on this PIN with no usable address: the extract cannot fill it, but it
    # does prove the parcel exists.
    _use_extract(monkeypatch, tmp_path, [{"Major": PIN[:6], "Minor": PIN[6:]}])
    _page(monkeypatch)
    gis = _gis(monkeypatch)

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert gis == [] and _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
async def test_a_gis_parcel_without_a_street_still_proves_it_exists(db, business_user, monkeypatch, tmp_path):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _page(monkeypatch)
    _gis(monkeypatch, body={"features": [{"attributes": {
        "PIN": PIN, "ADDR_FULL": None, "POSTALCTYNAME": "SEATTLE", "STATE_ABBR": "WA", "ZIP5": "98101"}}]})

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
@pytest.mark.parametrize("gis_kw", [
    {"status": 503},
    {"body": {"error": {"code": 500, "message": "Unable to complete operation."}}},
    {"raises": True},
], ids=["http-503", "arcgis-error-body", "connection-error"])
async def test_a_gis_layer_that_did_not_answer_proves_nothing(
        db, business_user, monkeypatch, tmp_path, gis_kw):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _page(monkeypatch)
    gis = _gis(monkeypatch, **gis_kw)

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert len(gis) == 1 and _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
async def test_a_capped_gis_page_proves_nothing(db, business_user, monkeypatch, tmp_path):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _page(monkeypatch)
    gis = _gis(monkeypatch, body={"features": [], "exceededTransferLimit": True})

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert len(gis) == 1 and _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline")
async def test_the_real_phase_one_answer_settles_end_to_end(db, business_user, monkeypatch, tmp_path):
    """No substituted pass: the real eRealProperty loop labels the page, the sweep settles."""
    erp: list = []

    def _erp_get(url, **kw):
        erp.append(url)
        return _Resp(200, NO_RECORD_PAGE)

    monkeypatch.setattr(kca, "safe_get", _erp_get)
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    gis = _gis(monkeypatch)

    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert len(erp) == 1 and erp[0].endswith(PIN) and len(gis) == 1 and stats["none"] == 1
    ed = await _ed(db, rid)
    assert ed["mailing_recovery_outcome"] == "parcel_not_found"
    assert ed["mailing_source"] == kca.NOT_CARRIED_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, "mismatch_unrepaired"], ids=["no-label", "other-label"])
async def test_a_page_that_was_not_the_no_record_answer_proves_nothing(
        db, business_user, monkeypatch, tmp_path, label):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _page(monkeypatch, label=label)
    gis = _gis(monkeypatch)

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert gis == [] and _rotated_uncharged(await _ed(db, rid))


@pytest.mark.asyncio
async def test_with_no_time_left_for_gis_it_rotates_instead(db, business_user, monkeypatch, tmp_path):
    rid = await _row(db, business_user)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _page(monkeypatch)
    gis = _gis(monkeypatch)
    monkeypatch.setattr(mr, "_GIS_PROOF_MIN_S", mr._TICK_BUDGET_S + 1)

    await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert gis == [] and _rotated_uncharged(await _ed(db, rid))


def test_one_proof_makes_at_most_one_gis_request(monkeypatch):
    pins = [f"99999{i:05d}" for i in range(60)]
    gis = _gis(monkeypatch)

    proven = kca.confirm_not_carried(pins, {p: kr.Answer("absent") for p in pins})

    # Only the first 50: one county_gis chunk, so one request inside its 30 s timeout.
    assert len(gis) == 1 and proven == set(pins[:50])


def test_no_usable_extract_proves_nothing_and_asks_gis_nothing(monkeypatch):
    gis = _gis(monkeypatch)

    assert kca.confirm_not_carried([PIN], None) == set()
    assert kca.confirm_not_carried([PIN], {PIN: kr.Answer("no_address")}) == set()
    assert gis == []


@pytest.mark.asyncio
async def test_settled_parcels_are_not_counted_as_unreached(db, business_user, monkeypatch, tmp_path):
    """One tick, three parcels: a mismatch, a proven missing PIN, and a plain failure."""
    mismatch, missing, failed = "9999990002", PIN, "9999990003"
    for parcel in (mismatch, missing, failed):
        await _row(db, business_user, parcel)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _gis(monkeypatch)
    labels = {mismatch: "mismatch", missing: "no_record"}

    async def _batch(parcels, **kw):
        kw["stats"].update({"requested_pids": list(parcels), "mailing_attempted_pids": []})
        return {p: {"property_address": None, "mailing_address": None, "parcel_lookup": labels[p]}
                for p in parcels if p in labels}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _batch)

    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert stats["parcel_mismatch"] == 1 and stats["none"] == 1
    assert stats["unreached"] == 1                     # only the plain failure is still open
