"""Mailing recovery settles a King PIN only when three King sources agree it does not exist.

Seen live 2026-10-31: the same 4 parcels (10 leads) were fetched every 10 minutes,
forever. Each page was eRealProperty's "No data found." answer, which phase 1 counts as
a failure; failures are never charged, and the breaker meant to stop a persistent
failure needs 30 of them, which a 4-parcel backlog never reaches.

Real DB, real rows, a real Assessor extract zip (tmp_path) read by the real reader, and
the real King GIS code with only its HTTP request substituted. The eRealProperty pass is
substituted as in test_mailing_recovery: what is under test is what the sweep WRITES.
"""
from __future__ import annotations

import asyncio
import csv
import io
import uuid
import zipfile

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.scrapers.enrichment import king_rpacct as kr
from src.workers import mailing_recovery as mr

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


def _use_extract(monkeypatch, tmp_path, accounts: list[dict]) -> None:
    """A real RPAcct zip, read by the real reader. `accounts` never names PIN unless asked."""
    cols = ["AcctNbr", "Major", "Minor", "AttnLine", "AddrLine", "CityState", "ZipCode"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for a in accounts:
        writer.writerow({c: a.get(c, "") for c in cols})
    path = tmp_path / "rpacct.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EXTR_RPAcct_NoName.csv", buf.getvalue().encode("latin-1"))
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (path, SNAPSHOT))


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
    assert ed["mailing_source"] == mr.NO_RECORD_SOURCE
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
async def test_the_real_phase_one_answer_settles_end_to_end(db, business_user, monkeypatch, tmp_path):
    """No substituted pass: the real eRealProperty loop labels the page, the sweep settles."""
    from src.scrapers.enrichment import king_county_assessor as kca
    from tests.test_king_no_record_page import NO_RECORD_PAGE, _Resp

    class _Lease:
        admitted = True

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def still_held(self):
            return True

    real_sleep = asyncio.sleep

    async def _no_pace(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _Lease)
    monkeypatch.setattr(kca, "check_source_or_raise", lambda *_a, **_k: None)
    monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)
    monkeypatch.setattr(kca.asyncio, "sleep", _no_pace)
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
    assert ed["mailing_source"] == mr.NO_RECORD_SOURCE


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
