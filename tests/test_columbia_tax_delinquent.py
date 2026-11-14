"""Columbia tax_delinquent and its connector migration (158), on the real test database.

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


@pytest.fixture
def db():
    with system_sync_session() as s:
        s.execute(text(_CLEAN))
        s.commit()
        yield s
        s.rollback()
        s.execute(text(_CLEAN))
        s.commit()


def _seed(db, parcel: str, *, situs: str, oldest: int = 2024, county: str = "columbia"):
    db.execute(text(
        "INSERT INTO pacs_tax_status (id, county, state, parcel_id, outcome, as_of, "
        "delinquent_amount, oldest_delinquent_year, owner_name, property_address, "
        "mailing_address, checked_at) VALUES (gen_random_uuid(), :c, 'WA', :p, 'found', "
        ":asof, 812.40, :old, 'OWNER REDACTED', :situs, 'PO BOX 1, DAYTON, WA 99328', :chk)"),
        {"c": county, "p": parcel, "asof": date(2026, 11, 14), "old": oldest, "situs": situs,
         "chk": datetime.now(UTC)})


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


# ─── Migration 158 ──────────────────────────────────────────────────────────
_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "158_columbia_seven_types.py"
_DECOY_ID = "57cd7eed-0000-4000-8000-000000000158"  # same county + URL, not the recorder


def _load():
    spec = importlib.util.spec_from_file_location("_mig158", _MIG)
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
    assert (mig.revision, mig.down_revision) == ("158", "157")
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
                                              mig._PACS_URL, True)
            assert rows[_DECOY_ID] == before  # matched by id, never by URL alone
            assert rows[mig._TAX_ID] == (mig._TAX_CLASS, '["tax_delinquent"]', mig._PACS_URL, False)
        _run(mig.downgrade)
        with system_sync_session() as s:
            assert {r[0]: r[1:] for r in _connectors(s)} == {mig._RECORDER_ID: before, _DECOY_ID: before}
    finally:
        with system_sync_session() as s:
            s.execute(text("DELETE FROM county_connectors WHERE county = 'columbia'"))
            s.commit()
        _run(mig.upgrade)  # leave the database as `alembic upgrade head` did (the tax seed)


def test_a_database_without_the_recorder_row_still_migrates():
    # The recorder row was made outside migrations: CI's fresh database has none.
    mig = _load()
    with system_sync_session() as s:
        s.execute(text("DELETE FROM county_connectors WHERE county = 'columbia'"))
        s.commit()
    _run(mig.upgrade)
    with system_sync_session() as s:
        assert [r[0] for r in _connectors(s)] == [mig._TAX_ID]
