"""Asotin probate: the scraper that reads probate_notices, and its connector (161).

Real database (TEST_DATABASE_URL). Notices are stored through the real crawl path
(workers/probate_ingest.ingest_text) from the redacted real fixtures, with the county
layer injected as in test_probate_ingest.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.asotin_wa_probate import DOC_TYPE, AsotinWAProbateScraper
from src.scrapers.enrichment.source_health import mark_source_healthy
from src.scrapers.probate import ProbateSignal, classify_probate_signal
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.trustee_sale import nts_crawl_heartbeat_key
from src.workers import probate_ingest as pi

_FIX = Path(__file__).parent / "fixtures"
_ROWS = [f["attributes"] for f in json.loads(
    (_FIX / "asotin_parcel_layer_owners.json").read_text())["features"]]
_NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
_HEARTBEAT = nts_crawl_heartbeat_key("asotin")
_CLEAN = ("DELETE FROM probate_notices WHERE county = 'asotin'",
          f"DELETE FROM external_source_health WHERE source_key = '{_HEARTBEAT}'")
_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / \
    "161_add_asotin_probate_connector.py"


@pytest.fixture
def db():
    with system_sync_session() as s:
        for sql in _CLEAN:
            s.execute(text(sql))
        s.commit()
        yield s
        s.rollback()
        for sql in _CLEAN:
            s.execute(text(sql))
        s.commit()


def _ingest(db, name: str) -> str | None:
    outcome = pi.ingest_text(
        db, (_FIX / f"{name}.txt").read_text(encoding="utf-8"), advert_id="2267",
        pdf_url="https://x/2267.pdf", now=_NOW,
        lookup=lambda surname: [r for r in _ROWS if surname in r["NAME"]],
        gate=lambda _parcels, _county: None)
    db.commit()
    return outcome


def _crawled(db):
    mark_source_healthy(db, _HEARTBEAT)
    db.commit()


async def _scrape(date_from="11/01/2026", date_to="11/14/2026"):
    return await AsotinWAProbateScraper().scrape(date_from, date_to)


async def test_one_lead_per_tied_parcel(db):
    assert _ingest(db, "asotin_probate_notice_a") == "matched"
    assert _ingest(db, "asotin_probate_notice_b") == "held"      # no tied parcel
    _crawled(db)
    records = await _scrape()
    assert len(records) == 1
    rec = records[0]
    assert rec.party_name == "HOLLOWAY, MARGARET J"
    assert rec.parcel_id == "10412300600050000"
    assert rec.property_address and rec.mailing_address
    assert rec.date_recorded == "11/4/2026" and rec.doc_type == DOC_TYPE
    # The personal representatives decided the tie only: never delivered.
    assert rec.heirs is None
    assert "DUNN" not in json.dumps(rec.to_dict())
    assert rec.enrichment_data["cause_no"] == "26-4-00122-02"
    assert rec.enrichment_data["parcel_ties"] == ["rep_shares_surname"]
    assert rec.enrichment_data["matched_by_name"] is True
    # Delivered as a real death/inheritance lead, never relabelled.
    assert classify_probate_signal(rec.doc_type) is ProbateSignal.DEATH_INHERITANCE
    # Stable identity: the same notice re-read gives the same lead.
    _ingest(db, "asotin_probate_notice_a")
    assert [r.raw_html_hash for r in await _scrape()] == [rec.raw_html_hash]


async def test_the_window_is_the_first_publication_date(db):
    _ingest(db, "asotin_probate_notice_a")                        # first published 11/4
    _crawled(db)
    assert await _scrape("11/05/2026", "11/14/2026") == []
    assert len(await _scrape("11/04/2026", "11/04/2026")) == 1


async def test_a_parcel_missing_mailing_is_held(db):
    _ingest(db, "asotin_probate_notice_a")
    db.execute(text(
        "UPDATE probate_notices SET parcels = CAST(:p AS json) WHERE cause_no = '26-4-00122-02'"),
        {"p": json.dumps([{"parcel_id": "10412300600050000", "party_name": "HOLLOWAY, MARGARET J",
                           "property_address": "1 MAIN ST", "mailing_address": "  "}])})
    db.commit()
    _crawled(db)
    assert await _scrape() == []


async def test_a_withheld_notice_is_never_delivered(db):
    _ingest(db, "asotin_probate_notice_a")
    db.execute(text("UPDATE probate_notices SET match_outcome = 'held' "
                    "WHERE cause_no = '26-4-00122-02'"))
    db.commit()
    _crawled(db)
    assert await _scrape() == []


async def test_an_empty_list_from_a_stale_crawl_fails_loudly(db):
    with pytest.raises(ScraperExecutionError, match="never been filled"):
        await _scrape()
    _crawled(db)
    assert await _scrape() == []                                   # fresh crawl: a real zero


async def test_a_notice_waiting_on_the_county_layer_is_not_a_zero(db):
    _ingest(db, "asotin_probate_notice_a")
    db.execute(text("UPDATE probate_notices SET match_outcome = 'failed', parcels = NULL "
                    "WHERE cause_no = '26-4-00122-02'"))
    db.commit()
    _crawled(db)                      # the newspaper itself was read: heartbeat is fresh
    with pytest.raises(ScraperExecutionError, match="waiting on the county parcel layer"):
        await _scrape()


async def test_a_window_older_than_retention_fails(db):
    _crawled(db)
    old = date.today() - timedelta(days=pi.RETENTION_DAYS + 30)
    mdy = lambda d: f"{d.month:02d}/{d.day:02d}/{d.year}"  # noqa: E731
    with pytest.raises(ScraperExecutionError, match="before the oldest notice kept"):
        await _scrape(mdy(old - timedelta(days=10)), mdy(old))


async def test_the_owner_of_record_with_a_representative_is_not_copied(db):
    # The county prints the representative inside the owner field ("C/O ... PR"): the
    # lead carries the decedent only, never that string.
    _ingest(db, "asotin_probate_notice_a")
    db.execute(text(
        "UPDATE probate_notices SET parcels = CAST(:p AS json) WHERE cause_no = '26-4-00122-02'"),
        {"p": json.dumps([{
            "parcel_id": "10412300600050000", "party_name": "WEBER, NORMA E",
            "owner_name": "WEBER, NORMA E ESTATE C/O GLENN L. WEBER PR",
            "property_address": "1 MAIN ST", "property_city": "CLARKSTON",
            "property_zip": "99403", "mailing_address": "PO BOX 1, CHATTAROY, WA 99003",
            "ties": ["estate_marked"]}])})
    db.commit()
    _crawled(db)
    [rec] = await _scrape()
    assert "GLENN" not in json.dumps(rec.to_dict())
    # Street only (a stable billing key); ZIP and state in their own fields.
    assert (rec.property_address, rec.property_zip, rec.property_state) == (
        "1 MAIN ST", "99403", "WA")


async def test_an_unusable_window_fails_instead_of_guessing(db):
    _crawled(db)
    with pytest.raises(ScraperExecutionError, match="unusable date window"):
        await _scrape("2026-11-01", "11/14/2026")
    with pytest.raises(ScraperExecutionError, match="unusable date window"):
        await _scrape("11/14/2026", "11/01/2026")


def _migration():
    spec = importlib.util.spec_from_file_location("_mig161", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_connector_chains_and_points_at_the_allowlisted_scraper():
    mig = _migration()
    assert (mig.revision, mig.down_revision) == ("161", "160")
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    assert getattr(importlib.import_module(module_path), class_name) is AsotinWAProbateScraper
    assert mig._COUNTY == "asotin"


def test_connector_is_seeded_inactive():
    # The public coverage count must not move until Asotin is approved for release.
    values = _MIG.read_text(encoding="utf-8").split("SELECT", 1)[1].split("WHERE NOT EXISTS")[0]
    assert values.strip().endswith("false")
    assert "'[\"probate\"]'" in values
