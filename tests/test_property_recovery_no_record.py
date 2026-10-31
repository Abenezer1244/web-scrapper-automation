"""Property recovery settles a King PIN that three King sources agree does not exist.

Before: eRealProperty's "No data found." page fell through to "transient", so each dead
PIN cost five charged ticks (five King requests) and ended as `gave_up`, losing the honest
answer. Now it settles as `parcel_not_found` on the mailing sweep's three proofs
(king_county_assessor.confirm_not_carried), and anything less stays transient as before.

The proof reads the extract from the worker's cache ONLY: a cold or stale cache would
mean an 18 MB download with a 300 s timeout inside a 300 s tick, so it proves nothing
instead, and the PIN stays transient.

Real DB, a real Assessor extract zip in a real worker cache (only the download is
substituted, and asserted never to happen), the real King GIS code with only its HTTP
request substituted. Most cases substitute the page pass as test_property_recovery does;
one runs the real eRealProperty pass with only its HTTP response substituted.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from sqlalchemy import text

from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from src.workers import property_recovery as prc
from tests.test_king_no_record_page import NO_RECORD_PAGE, _Resp, offline  # noqa: F401  (fixture)
from tests.test_mailing_recovery_no_record import (
    _OTHER_ACCOUNT,
    PIN,
    SNAPSHOT,
    _gis,
    _real_cache,
    _use_extract,
)
from tests.test_property_recovery import _get, _job, _lead, _page, _tick

pytestmark = pytest.mark.asyncio

_NO_RECORD = {"property_address": None, "mailing_address": None, "owner_name": None,
              "parcel_lookup": "no_record"}


@pytest.fixture(autouse=True)
def _enabled_and_healthy(monkeypatch):
    from src.config import settings
    from src.db.session import SyncSessionLocal

    monkeypatch.setattr(settings, "PROPERTY_RECOVERY_ENABLED", True, raising=False)

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


async def test_three_agreeing_sources_settle_it_and_it_is_never_asked_again(
        db, business_user, monkeypatch, tmp_path):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    downloads = _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    gis = _gis(monkeypatch)
    _page(monkeypatch, lambda p: _NO_RECORD)

    stats = await asyncio.to_thread(_tick)

    assert stats["parcel_not_found"] == 1 and stats["transient"] == 0 and len(gis) == 1
    assert downloads == []
    ed = (await _get(db, rid)).enrichment_data
    assert ed["property_lookup_outcome"] == "parcel_not_found"
    assert ed["property_lookup_deferred"] is False and ed["property_recovery_attempts"] == 1
    assert ed["property_source"] == kca.NOT_CARRIED_SOURCE
    assert ed["property_source_snapshot"] == SNAPSHOT

    asked = _page(monkeypatch, lambda p: _NO_RECORD)
    await asyncio.to_thread(_tick)
    assert asked == []                                     # settled: never fetched again


async def _stays_transient(db, rid) -> None:
    ed = (await _get(db, rid)).enrichment_data
    assert ed["property_lookup_outcome"] == "transient_failure"
    assert ed["property_lookup_deferred"] is True and ed["property_recovery_attempts"] == 1


async def test_a_cold_cache_is_never_downloaded_and_proves_nothing(db, business_user, monkeypatch, tmp_path):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    downloads = _real_cache(monkeypatch, tmp_path / "empty_cache")
    gis = _gis(monkeypatch)
    _page(monkeypatch, lambda p: _NO_RECORD)

    stats = await asyncio.to_thread(_tick)

    assert downloads == [] and gis == [] and stats["transient"] == 1
    await _stays_transient(db, rid)


async def test_a_stale_cache_is_never_refreshed_and_proves_nothing(db, business_user, monkeypatch, tmp_path):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    downloads = _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(tmp_path / "rpacct_cache" / "rpacct.zip", (two_days_ago, two_days_ago))
    gis = _gis(monkeypatch)
    _page(monkeypatch, lambda p: _NO_RECORD)

    await asyncio.to_thread(_tick)

    assert downloads == [] and gis == []
    await _stays_transient(db, rid)


@pytest.mark.usefixtures("offline")
async def test_the_real_page_pass_settles_end_to_end(db, business_user, monkeypatch, tmp_path):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    gis = _gis(monkeypatch)
    erp: list = []

    def _erp_get(url, **kw):
        erp.append(url)
        return _Resp(200, NO_RECORD_PAGE)

    monkeypatch.setattr(kca, "safe_get", _erp_get)

    stats = await asyncio.to_thread(_tick)

    assert len(erp) == 1 and erp[0].endswith(PIN) and len(gis) == 1
    assert stats["parcel_not_found"] == 1
    assert (await _get(db, rid)).enrichment_data["property_lookup_outcome"] == "parcel_not_found"


async def test_a_gis_parcel_proves_it_exists(db, business_user, monkeypatch, tmp_path):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _gis(monkeypatch, body={"features": [{"attributes": {
        "PIN": PIN, "ADDR_FULL": None, "POSTALCTYNAME": "SEATTLE", "STATE_ABBR": "WA", "ZIP5": "98101"}}]})
    _page(monkeypatch, lambda p: _NO_RECORD)

    await asyncio.to_thread(_tick)

    await _stays_transient(db, rid)


@pytest.mark.parametrize("floor", ["_EXTRACT_PROOF_MIN_S", "_GIS_PROOF_MIN_S"])
async def test_with_no_time_left_it_stays_transient_and_asks_gis_nothing(
        db, business_user, monkeypatch, tmp_path, floor):
    rid = await _lead(db, business_user, await _job(db, business_user), PIN)
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    gis = _gis(monkeypatch)
    _page(monkeypatch, lambda p: _NO_RECORD)
    monkeypatch.setattr(prc, floor, prc._TICK_BUDGET_S + 1)

    await asyncio.to_thread(_tick)

    assert gis == []
    await _stays_transient(db, rid)


async def test_a_job_never_re_marks_a_settled_parcel(db, business_user):
    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.tasks_helpers.enrich import _mark_king_property_deferred

    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, PIN, marked=False)

    def _mark() -> int:
        with SyncSessionLocal() as job_db, SyncSessionLocal() as sweep_db:
            row = job_db.get(Result, rid)          # the job's copy: not settled
            sweep_db.execute(text(                 # the sweep settles it meanwhile
                "UPDATE results SET enrichment_data = (enrichment_data::jsonb "
                "|| '{\"property_lookup_outcome\": \"parcel_not_found\"}'::jsonb)::json WHERE id = :i"),
                {"i": rid})
            sweep_db.commit()
            # Only the SQL guard can stop the re-mark: the ORM copy still says unsettled.
            n = _mark_king_property_deferred(job_db, [row], job_id, str(business_user.id))
            job_db.commit()
            return n

    assert await asyncio.to_thread(_mark) == 0
    ed = (await _get(db, rid)).enrichment_data
    assert ed["property_lookup_outcome"] == "parcel_not_found" and "property_lookup_deferred" not in ed
