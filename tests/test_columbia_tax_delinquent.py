"""Columbia tax_delinquent and its connector migration (160), on the real test database.

The reader is Asotin's (pacs_tax_status cache) with Columbia's county and source. New
rule for both: a situs without a house number is not a property address. Live
2026-11-14, a rural Columbia parcel's PACS situs was only "DAYTON, WA 99328".
"""
from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.columbia_wa_tax_delinquent import ColumbiaWATaxDelinquentScraper
from src.scrapers.reliability import ScraperExecutionError

_CLEAN = "DELETE FROM pacs_tax_status WHERE county IN ('columbia', 'asotin')"
_CLEAN_INV = ("DELETE FROM external_source_health WHERE source_key IN "
              "('pacs_tax_inventory:columbia', 'pacs_tax_inventory:asotin')")


@pytest.fixture
def db():
    with system_sync_session() as s:
        s.execute(text(_CLEAN))
        s.execute(text(_CLEAN_INV))
        s.commit()
        yield s
        s.rollback()
        s.execute(text(_CLEAN))
        s.execute(text(_CLEAN_INV))
        s.commit()


def _seed(db, parcel: str, *, situs: str, oldest: int = 2024, county: str = "columbia"):
    db.execute(text(
        "INSERT INTO pacs_tax_status (id, county, state, parcel_id, outcome, as_of, "
        "delinquent_amount, oldest_delinquent_year, owner_name, property_address, "
        "mailing_address, checked_at) VALUES (gen_random_uuid(), :c, 'WA', :p, 'found', "
        ":asof, 812.40, :old, 'OWNER REDACTED', :situs, 'PO BOX 1, DAYTON, WA 99328', :chk)"),
        {"c": county, "p": parcel, "asof": date(2026, 11, 14), "old": oldest, "situs": situs,
         "chk": datetime.now(UTC)})
    from src.scrapers.enrichment.source_health import mark_source_healthy

    mark_source_healthy(db, f"pacs_tax_inventory:{county}")  # the crawl's weekly inventory


async def test_only_parcels_with_a_house_numbered_situs_are_delivered(db):
    _seed(db, "1180050890000", situs="500 S 5TH ST, DAYTON, WA 99328")       # delivered
    _seed(db, "2600030000000", situs="DAYTON, WA 99328")                     # held: locality only
    _seed(db, "2009390114700", situs="WOLF FORK RD, DAYTON, WA 99328")       # held: no number
    _seed(db, "1170050110001", situs="721 E TREMONT ST, DAYTON, WA 99328", oldest=2026)  # current year
    db.commit()
    records = await ColumbiaWATaxDelinquentScraper().scrape("01/01/2026", "11/14/2026")
    assert [r.parcel_id for r in records] == ["1180050890000"]
    assert records[0].enrichment_data["source"] == "columbia_pacs_tax"
    assert records[0].enrichment_data["delinquent_amount"] == "812.40"


async def test_columbia_never_reads_another_countys_cache(db):
    _seed(db, "10014003000000000", situs="1 TEST ST, CLARKSTON, WA 99403", county="asotin")
    db.commit()
    with pytest.raises(ScraperExecutionError):  # Columbia's own cache is empty: not a zero
        await ColumbiaWATaxDelinquentScraper().scrape("01/01/2026", "11/14/2026")


@pytest.mark.parametrize(("situs", "ok"), [
    ("500 S 5TH ST, DAYTON, WA 99328", True),
    ("12A MAIN ST, DAYTON, WA 99328", True),
    ("12-B MAIN ST, DAYTON, WA 99328", True),
    ("123 1/2 MAIN ST, DAYTON, WA 99328", True),
    ("1ST ST, CLARKSTON, WA 99403", False),      # an ordinal road name, no house number
    ("99328", False),                             # a ZIP
    ("DAYTON, WA 99328", False),                  # locality only
    ("WOLF FORK RD, DAYTON, WA 99328", False),    # road, no number
    ("", False),
])
def test_a_property_address_needs_a_house_number(situs, ok):
    from types import SimpleNamespace

    from src.scrapers.asotin_wa_tax_delinquent import deliverable

    row = SimpleNamespace(owner_name="OWNER", mailing_address="PO BOX 1, DAYTON, WA 99328",
                          property_address=situs)
    assert bool(deliverable(row)) is ok


def test_columbia_is_crawled_scoped_and_trusted():
    from src.scrapers.enrichment.county_gis import _wa_fips_filter
    from src.workers import pacs_tax_crawler
    from src.workers.tasks_helpers.dedup import _TRUSTED_TAX_SOURCES

    assert "columbia" in pacs_tax_crawler.COUNTIES
    assert _wa_fips_filter("columbia") == " AND FIPS_NR='013'"
    assert ColumbiaWATaxDelinquentScraper.SOURCE in _TRUSTED_TAX_SOURCES


# ─── Migration 160 ──────────────────────────────────────────────────────────
_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "160_columbia_seven_types.py"
_DECOY_ID = "57cd7eed-0000-4000-8000-000000000158"  # same county + URL, not the recorder


def _load():
    spec = importlib.util.spec_from_file_location("_mig160", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(step):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from src.db.session import system_sync_session as sess

    with sess() as s:
        with Operations.context(MigrationContext.configure(s.connection())):
            step()
        s.commit()


def _connectors(s):
    return s.execute(text(
        "SELECT id::text, scraper_class, record_types::text, assessor_url, active FROM county_connectors "
        "WHERE county = 'columbia' ORDER BY scraper_class, id")).all()


def test_migration_seed_points_at_an_allowlisted_columbia_reader():
    from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES

    mig = _load()
    assert (mig.revision, mig.down_revision) == ("160", "159")
    module_path, name = mig._TAX_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    assert getattr(importlib.import_module(module_path), name).COUNTY == "columbia"


def test_migration_upgrades_the_recorder_seeds_tax_inactive_and_reverts():
    mig = _load()
    with system_sync_session() as s:
        s.execute(text("DELETE FROM county_connectors WHERE county = 'columbia'"))
        for cid in (mig._RECORDER_ID, _DECOY_ID):
            s.execute(text(
                "INSERT INTO county_connectors (id, county, state, record_types, scraper_class, "
                "scraper_mode, render_mode, base_url, health_status, active) VALUES (:id, 'columbia', "
                "'wa', '[\"probate\", \"pre_foreclosure\"]', '', 'template', 'playwright', :url, "
                "'down', true)"), {"id": cid, "url": mig._RECORDER_URL})
        s.commit()
    before = ("", '["probate", "pre_foreclosure"]', None, True)
    try:
        _run(mig.upgrade)
        _run(mig.upgrade)  # idempotent
        with system_sync_session() as s:
            rows = {r[0]: r[1:] for r in _connectors(s)}
            assert rows[mig._RECORDER_ID] == ("", '["probate", "pre_foreclosure", "death_certificate"]',
                                              None, True)
            assert rows[_DECOY_ID] == before  # matched by id, never by URL alone
            assert rows[mig._TAX_ID] == (mig._TAX_CLASS, '["tax_delinquent"]', None, False)
        _run(mig.downgrade)
        with system_sync_session() as s:
            assert {r[0]: r[1:] for r in _connectors(s)} == {mig._RECORDER_ID: before, _DECOY_ID: before}
    finally:
        with system_sync_session() as s:
            s.execute(text("DELETE FROM county_connectors WHERE county = 'columbia'"))
            s.commit()
        _run(mig.upgrade)  # leave the database as `alembic upgrade head` did (the tax seed)


def test_columbia_never_gets_an_owner_name_lookup():
    # Owner decision 2026-11-14: Columbia recorder leads get a property only from their
    # recorded legal description, never from a name match. The name lookup runs only
    # with a PACS assessor_url (connector row, else this built-in map).
    from src.scrapers.enrichment.assessor_urls import KNOWN_ASSESSOR_URLS

    assert "columbia_WA" not in KNOWN_ASSESSOR_URLS
    assert "assessor_url = '" not in _MIG.read_text(encoding="utf-8")


def test_a_database_without_the_recorder_row_still_migrates():
    # The recorder row was made outside migrations: CI's fresh database has none.
    mig = _load()
    with system_sync_session() as s:
        s.execute(text("DELETE FROM county_connectors WHERE county = 'columbia'"))
        s.commit()
    _run(mig.upgrade)
    with system_sync_session() as s:
        assert [r[0] for r in _connectors(s)] == [mig._TAX_ID]


# ─── Final-review fixes: unknown is never "paid", short or stale reads never count ─────
_FIX = Path(__file__).parent / "fixtures"
_GEO = "1180050890000"
_CLEAN_HEALTH = "DELETE FROM external_source_health WHERE source_key = 'pacs_tax_inventory:columbia'"


def _detail_page() -> str:
    return (_FIX / f"columbia_pacs_detail_{_GEO}.html").read_text(encoding="utf-8")


def test_a_real_columbia_page_has_statements():
    from src.scrapers.enrichment import pacs_tax

    answer = pacs_tax.parse_tax(_detail_page(), _GEO)
    assert answer.outcome == "found" and len(answer.statements) == 2


def test_a_statement_table_without_rows_is_unparsed_not_paid():
    from src.scrapers.enrichment import pacs_tax

    drifted = _detail_page().replace("rowEnd", "rowX")  # the row class renamed
    assert pacs_tax.parse_tax(drifted, _GEO).outcome == "unparsed"


def _layer(n: int, counted: int):
    feats = [{"attributes": {"ORIG_PARCEL_ID": str(1180050890000 + i)}} for i in range(n)]
    return lambda url: {"count": counted} if "returnCountOnly" in url else {"features": feats}


def test_an_inventory_shorter_than_the_layers_count_fails():
    from src.workers import pacs_tax_crawler as crawler

    assert len(crawler.statewide_parcel_ids("columbia", _layer(3, 3))) == 3
    with pytest.raises(ValueError, match="counted 5"):
        crawler.statewide_parcel_ids("columbia", _layer(3, 5))


def test_a_stale_page_is_asked_but_never_settled(db):
    from src.scrapers.enrichment import pacs_tax
    from src.workers import pacs_tax_crawler as crawler

    db.execute(text(_CLEAN_HEALTH))
    now = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)

    def resolve(county, parcel_ids, *, time_budget_s):
        old = pacs_tax.parse_tax(_detail_page(), _GEO)  # as of 11/14/2026
        old.as_of = date(2026, 10, 1)                   # the county serving an old snapshot
        return dict.fromkeys(parcel_ids, old)

    summary = crawler.crawl_county("columbia", db, fetch=_layer(1, 1), resolve=resolve, now=now)
    assert (summary["asked"], summary["settled"]) == (1, 0)
    row = db.execute(text("SELECT checked_at, attempted_at FROM pacs_tax_status "
                          "WHERE county = 'columbia'")).one()
    assert row.checked_at is None and row.attempted_at is not None
    db.execute(text(_CLEAN_HEALTH))
    db.commit()


def test_a_crawl_that_learns_nothing_raises_the_silence_alarm(db):
    from datetime import timedelta

    from src.workers import pacs_tax_crawler as crawler

    db.execute(text(_CLEAN_HEALTH))
    db.commit()
    now = datetime.now(UTC)
    assert crawler.silent_since("columbia", db, now) is None  # no inventory yet: nothing to say

    def unavailable(county, parcel_ids, *, time_budget_s):  # e.g. the year selector vanished
        from src.scrapers.enrichment import pacs_tax
        return {pid: pacs_tax.TaxAnswer("source_unavailable") for pid in parcel_ids}

    crawler.crawl_county("columbia", db, fetch=_layer(2, 2), resolve=unavailable, now=now)
    assert crawler.silent_since("columbia", db, now) is None             # just loaded
    assert crawler.silent_since("columbia", db, now + timedelta(hours=7)) is not None
    db.execute(text(_CLEAN_HEALTH))
    db.commit()


async def test_an_inventory_not_refreshed_for_two_weeks_is_not_an_answer(db):
    from datetime import timedelta

    _seed(db, "1180050890000", situs="500 S 5TH ST, DAYTON, WA 99328")
    db.execute(text("UPDATE external_source_health SET last_success_at = :t "
                    "WHERE source_key = 'pacs_tax_inventory:columbia'"),
               {"t": datetime.now(UTC) - timedelta(days=15)})
    db.commit()
    with pytest.raises(ScraperExecutionError, match="inventory"):
        await ColumbiaWATaxDelinquentScraper().scrape("01/01/2026", "11/14/2026")


def _layer_with_ids(ids: list[str]):
    feats = [{"attributes": {"ORIG_PARCEL_ID": i}} for i in ids]
    return lambda url: {"count": len(feats)} if "returnCountOnly" in url else {"features": feats}


def test_blank_and_duplicate_features_are_handled_explicitly():
    from src.workers import pacs_tax_crawler as crawler

    # The live shape: duplicates for multi-part parcels, a few features with no id.
    ids = [str(1180050890000 + i) for i in range(200)] + ["1180050890000", None]
    assert len(crawler.statewide_parcel_ids("columbia", _layer_with_ids(ids))) == 200
    # A renamed or emptied id field: most features blank, never a smaller inventory.
    with pytest.raises(ValueError, match="no parcel id"):
        crawler.statewide_parcel_ids("columbia", _layer_with_ids(["1180050890000"] + [None] * 9))


def test_an_empty_inventory_fails_and_keeps_the_last_heartbeat(db):
    from src.scrapers.enrichment.source_health import get_source_state, mark_source_healthy
    from src.workers import pacs_tax_crawler as crawler

    with pytest.raises(ValueError, match="no parcels"):
        crawler.statewide_parcel_ids("columbia", _layer_with_ids([]))
    mark_source_healthy(db, "pacs_tax_inventory:columbia")
    db.commit()
    before = get_source_state(db, "pacs_tax_inventory:columbia")["last_success_at"]
    # A refresh that is due (8 days later) and reads an empty layer: the run fails and
    # the heartbeat does not move, so the 14-day reader gate still expires on time.
    from datetime import timedelta

    later = datetime.now(UTC) + timedelta(days=8)
    with pytest.raises(ValueError):
        crawler.crawl_county("columbia", db, fetch=_layer_with_ids([]),
                             resolve=lambda *a, **k: {}, now=later)
    db.rollback()
    assert get_source_state(db, "pacs_tax_inventory:columbia")["last_success_at"] == before
