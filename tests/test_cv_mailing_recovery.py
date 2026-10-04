"""Background mailing for King code-violation leads the job's locate step never reached.

Contract pinned here: only done King code-violation rows with coordinates, no parcel_id,
no mailing and no kc_pin_status; a decided row gets the job's own kc_* keys (+ mailing
from the extract) and is never asked again; parcel_id stays NULL; a row with no answer
is charged one attempt and settles as gave_up; a batch with no answer at all stands the
step down on the source-health ladder without charging anyone; the kill switch and the
Redis lock stop it before any request.

Real DB, real Redis, real sweep. Only King's parcel-layer HTTP response and the
Assessor extract file are substituted (same fakes as test_king_code_violation_mailing).
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment import king_rpacct as kr
from src.scrapers.enrichment.source_health import KING_CV_PARCEL_LOCATE
from src.workers import cv_mailing_recovery as cmr
from tests.test_king_code_violation_mailing import P0904, _cv_row, _layer, _Resp
from tests.test_king_rpacct_mailing import _acct, _extract

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    from src.config import settings
    from src.db.session import SyncSessionLocal

    monkeypatch.setattr(cmr, "_PACE_S", 0.0)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_CV_PARCEL_LOCATE})
            sdb.commit()
        import redis as sync_redis

        sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()).delete(cmr._LOCK_KEY)

    _wipe()
    yield
    _wipe()


def _with_extract(monkeypatch, tmp_path):
    zp = _extract(tmp_path, [_acct("090400", "0025", "PO BOX 5003", "BELLEVUE WA", "98009")])
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: (zp, "2026-09-05"))


def _layer_down(monkeypatch) -> list:
    calls: list = []

    def _get(*a, **kw):
        calls.append(1)
        return _Resp({"error": {"code": 503}})

    monkeypatch.setattr(kpl, "safe_get", _get)
    return calls


async def _tick() -> dict:
    return await asyncio.to_thread(cmr.recover_code_violation_mailing)


async def _row(db, rid: str):
    return (await db.execute(text(
        "SELECT parcel_id, mailing_address, owner_state, absentee_owner, enrichment_data "
        "FROM results WHERE id = :id"), {"id": rid})).one()


async def test_a_reached_row_gets_its_parcel_and_mailing_and_is_never_asked_again(
    db, business_user, tmp_path, monkeypatch,
):
    done = await _cv_row(db, business_user)
    live = await _cv_row(db, business_user, status="enriching")
    _layer(monkeypatch, P0904)
    _with_extract(monkeypatch, tmp_path)

    stats = await _tick()
    assert (stats["rows"], stats["found"], stats["errors"]) == (1, 1, 0)

    got = await _row(db, done)
    assert got.mailing_address == "PO BOX 5003, BELLEVUE, WA 98009"
    assert got.parcel_id is None                       # the PIN never becomes the identity
    assert got.enrichment_data["kc_pin"] == "0904000025"
    assert got.enrichment_data["kc_pin_status"] == "matched"
    assert got.enrichment_data["mailing_source"] == "king_rpacct"
    assert got.enrichment_data["record_number"] == "000630-26CP"   # merged, not replaced
    assert got.owner_state == "WA" and got.absentee_owner is True
    assert (await _row(db, live)).mailing_address is None          # job not done: untouched

    assert (await _tick())["rows"] == 0                            # decided: never re-asked


async def test_a_row_with_no_answer_is_charged_and_settles_as_gave_up(
    db, business_user, tmp_path, monkeypatch,
):
    rid = await _cv_row(db, business_user)
    _with_extract(monkeypatch, tmp_path)
    _layer_down(monkeypatch)                     # one row: below the breaker's batch size

    for attempt in range(1, cmr._MAX_ATTEMPTS + 1):
        stats = await _tick()
        assert stats["rows"] == 1
        ed = (await _row(db, rid)).enrichment_data
        assert ed[cmr.ATTEMPTS_KEY] == attempt
    assert stats["gave_up"] == 1 and ed[cmr.OUTCOME_KEY] == "gave_up"
    assert "kc_pin_status" not in ed and (await _row(db, rid)).mailing_address is None
    assert (await _tick())["rows"] == 0                            # settled: left alone


async def test_a_batch_with_no_answer_stands_the_step_down_without_charging_anyone(
    db, business_user, tmp_path, monkeypatch,
):
    ids = [await _cv_row(db, business_user, lat=f"47.6{i}", lon="-122.4")
           for i in range(cmr._BREAKER_MIN_ROWS)]
    _with_extract(monkeypatch, tmp_path)
    calls = _layer_down(monkeypatch)

    stats = await _tick()
    assert stats["skipped"].startswith("stopped: no answer for")
    for rid in ids:
        assert cmr.ATTEMPTS_KEY not in (await _row(db, rid)).enrichment_data

    asked = len(calls)
    again = await _tick()
    assert again["skipped"] == f"{KING_CV_PARCEL_LOCATE} is in cooldown"
    assert len(calls) == asked                                     # no request while cooling


async def test_the_kill_switch_and_the_lock_stop_it_before_any_request(db, business_user, monkeypatch):
    from src.config import settings

    await _cv_row(db, business_user)
    calls = _layer_down(monkeypatch)

    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", False, raising=False)
    assert (await _tick())["skipped"] == "GIS_ENRICHMENT_ENABLED is off"
    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)

    import redis as sync_redis

    sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()).set(cmr._LOCK_KEY, "other", ex=60)
    assert (await _tick())["skipped"] == "another tick is running"
    assert calls == []


async def test_printed_pin_sources_and_parcelled_rows_are_never_located(
    db, business_user, tmp_path, monkeypatch,
):
    from src.scrapers.king_cv_sources import PARCEL_AT_SCRAPE_SOURCES

    printed = await _cv_row(db, business_user)
    await db.execute(text(
        "UPDATE results SET enrichment_data = (enrichment_data::jsonb || CAST(:p AS jsonb))::json "
        "WHERE id = :id"), {"id": printed,
                            "p": f'{{"source": "{sorted(PARCEL_AT_SCRAPE_SOURCES)[0]}"}}'})
    await db.commit()
    parcelled = await _cv_row(db, business_user, parcel="0904000025")
    _layer(monkeypatch, P0904)
    _with_extract(monkeypatch, tmp_path)

    assert (await _tick())["rows"] == 0
    for rid in (printed, parcelled):
        assert (await _row(db, rid)).mailing_address is None


async def test_an_unusable_extract_stands_down_before_asking_king(db, business_user, monkeypatch):
    """Without the extract every matched row would silently lose its decision and be
    charged toward gave_up while other outcomes looked healthy (Codex P1)."""
    rid = await _cv_row(db, business_user)
    calls = _layer_down(monkeypatch)
    monkeypatch.setattr(kr, "cached_extract", lambda *a, **kw: None)

    stats = await _tick()
    assert stats["skipped"] == "stopped: Assessor extract unavailable"
    assert calls == [] and cmr.ATTEMPTS_KEY not in (await _row(db, rid)).enrichment_data
    assert (await _tick())["skipped"] == f"{KING_CV_PARCEL_LOCATE} is in cooldown"


async def test_a_failing_lookup_stands_down_instead_of_re_asking_every_hour(
    db, business_user, tmp_path, monkeypatch,
):
    rid = await _cv_row(db, business_user)
    _with_extract(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("layer exploded")

    monkeypatch.setattr(kpl, "resolve_code_violation_mailing", _boom)

    stats = await _tick()
    assert stats["skipped"].startswith("stopped: lookup failed: RuntimeError")
    assert cmr.ATTEMPTS_KEY not in (await _row(db, rid)).enrichment_data
    assert (await _tick())["skipped"] == f"{KING_CV_PARCEL_LOCATE} is in cooldown"


async def test_a_lead_edited_during_the_lookup_gets_nothing(db, business_user, tmp_path, monkeypatch):
    """The answer belongs to the coordinates and address it was asked for."""
    from src.db.session import SyncSessionLocal

    rid = await _cv_row(db, business_user)
    _with_extract(monkeypatch, tmp_path)

    def _get(*a, **kw):
        with SyncSessionLocal() as sdb:          # someone moves the lead mid-lookup
            sdb.execute(text(
                "UPDATE results SET enrichment_data = (enrichment_data::jsonb "
                "|| '{\"latitude\": \"47.70000000\"}'::jsonb)::json WHERE id = :id"), {"id": rid})
            sdb.commit()
        return _Resp({"features": [{"attributes": P0904}]})

    monkeypatch.setattr(kpl, "safe_get", _get)
    stats = await _tick()
    assert (stats["stale"], stats["found"]) == (1, 0)
    got = await _row(db, rid)
    assert got.mailing_address is None and "kc_pin" not in got.enrichment_data
