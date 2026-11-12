"""Asotin tax_delinquent: the rolling PACS tax crawl and the cache-reading scraper.

Real database (TEST_DATABASE_URL), real fixtures: a real statewide-layer answer for
three Asotin parcels and real PACS detail pages (owner names redacted). The crawl's
two network edges (statewide layer, PACS pass) are injected, exactly as the NTS
crawlers inject their fetchers.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.asotin_wa_tax_delinquent import AsotinWATaxDelinquentScraper
from src.scrapers.enrichment import pacs_tax
from src.scrapers.reliability import ScraperExecutionError
from src.workers import pacs_tax_crawler as crawler

_FIX = Path(__file__).parent / "fixtures"
_OWING, _PAID, _UNREACHED = "10014003000000000", "10040300100010000", "10040300800020000"
_PAGES = {_OWING: "asotin_pacs_detail_1651.html", _PAID: "asotin_pacs_detail_2712.html"}


_CLEAN = ("DELETE FROM pacs_tax_status WHERE county = 'asotin'",
          "DELETE FROM external_source_health WHERE source_key = 'pacs_tax_inventory:asotin'")


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


def _fetch(url: str) -> dict:
    return json.loads((_FIX / "wa_statewide_asotin_3_parcels.json").read_text(encoding="utf-8"))


def _resolve(county, parcel_ids, *, time_budget_s):
    out = {}
    for pid in parcel_ids:
        page = _PAGES.get(pid)
        out[pid] = (pacs_tax.parse_tax((_FIX / page).read_text(encoding="utf-8"), pid) if page
                    else pacs_tax.TaxAnswer("source_unavailable"))
    return out


def _rows(db) -> dict:
    found = db.execute(text("SELECT * FROM pacs_tax_status WHERE county = 'asotin'"))
    return {m["parcel_id"]: SimpleNamespace(**m) for m in found.mappings().all()}


class TestCrawl:
    def test_inventory_then_one_pass(self, db):
        summary = crawler.crawl_county("asotin", db, fetch=_fetch, resolve=_resolve)
        assert summary["inventory_added"] == 3
        assert (summary["asked"], summary["settled"], summary["owing"]) == (2, 2, 1)
        rows = _rows(db)
        owing, paid, unreached = rows[_OWING], rows[_PAID], rows[_UNREACHED]
        assert owing.delinquent_amount == Decimal("2270.78")
        assert owing.oldest_delinquent_year == 2026 and owing.as_of == date(2026, 11, 12)
        assert owing.owner_name == "OWNER REDACTED" and owing.checked_at is not None
        # A paid-up parcel keeps no name or address: only owing parcels are leads.
        assert paid.delinquent_amount == Decimal("0") and paid.owner_name is None
        assert paid.mailing_address is None and paid.checked_at is not None
        # Never asked (lease lost / budget): untouched, still first in line.
        assert unreached.attempted_at is None and unreached.checked_at is None

    def test_a_failed_answer_keeps_the_last_good_row(self, db):
        crawler.crawl_county("asotin", db, fetch=_fetch, resolve=_resolve)
        before = _rows(db)[_OWING]

        def broken(county, parcel_ids, *, time_budget_s):
            return {pid: pacs_tax.TaxAnswer("unparsed") for pid in parcel_ids}

        crawler.crawl_county("asotin", db, fetch=_fetch, resolve=broken)
        after = _rows(db)[_OWING]
        assert after.delinquent_amount == before.delinquent_amount
        assert after.checked_at == before.checked_at      # freshness did not move
        assert after.attempted_at > before.attempted_at   # but it rotated back

    def test_a_page_for_another_parcel_is_not_an_answer(self, db):
        crawler.crawl_county("asotin", db, fetch=_fetch, resolve=_resolve)
        before = _rows(db)[_OWING]

        def mismatch(county, parcel_ids, *, time_budget_s):
            return {pid: pacs_tax.TaxAnswer("parcel_mismatch") for pid in parcel_ids}

        crawler.crawl_county("asotin", db, fetch=_fetch, resolve=mismatch)
        after = _rows(db)[_OWING]
        assert (after.delinquent_amount, after.outcome) == (before.delinquent_amount, "found")
        assert after.checked_at == before.checked_at  # never counted as coverage

    def test_the_inventory_is_read_once_a_week_not_every_run(self, db):
        crawler.crawl_county("asotin", db, fetch=_fetch, resolve=_resolve)

        def no_fetch(url):
            raise AssertionError("inventory re-read inside its week")

        crawler.crawl_county("asotin", db, fetch=no_fetch, resolve=_resolve)

    def test_a_layer_without_features_is_an_error_not_an_empty_county(self):
        with pytest.raises(ValueError):
            crawler.statewide_parcel_ids("asotin", lambda url: {"error": {"code": 400}})


def _seed(db, parcel: str, *, owing: str, oldest: int | None, owner=True, mailing=True,
          checked=True):
    db.execute(text(
        "INSERT INTO pacs_tax_status (id, county, state, parcel_id, outcome, as_of, "
        "delinquent_amount, oldest_delinquent_year, owner_name, property_address, "
        "mailing_address, checked_at) VALUES (gen_random_uuid(), 'asotin', 'WA', :p, "
        "'found', :asof, :amt, :old, :own, :situs, :mail, :chk)"),
        {"p": parcel, "asof": date(2026, 11, 12), "amt": Decimal(owing), "old": oldest,
         "own": "OWNER REDACTED" if owner else None, "situs": "1 TEST ST, CLARKSTON, WA 99403",
         "mail": "PO BOX 1, CLARKSTON, WA 99403" if mailing else None,
         "chk": datetime.now(UTC) if checked else datetime.now(UTC) - timedelta(days=30)})


class TestScraper:
    async def test_delivers_prior_year_debt_and_holds_incomplete_leads(self, db):
        _seed(db, "1", owing="900.00", oldest=2025)                 # delivered
        _seed(db, "2", owing="2270.78", oldest=2026)                # current year only
        _seed(db, "3", owing="500.00", oldest=2024, mailing=False)  # held: no mailing
        _seed(db, "4", owing="0", oldest=None)                      # paid up
        db.commit()
        records = await AsotinWATaxDelinquentScraper().scrape("01/01/2026", "11/12/2026")
        assert [r.parcel_id for r in records] == ["1"]
        rec = records[0]
        assert rec.enrichment_data["source"] == "asotin_pacs_tax"
        assert rec.enrichment_data["bill_year"] == 2025
        assert rec.enrichment_data["delinquent_amount"] == "900.00"
        assert rec.date_recorded == "11/12/2026"

    async def test_a_stale_or_partial_cache_fails_instead_of_answering(self, db):
        _seed(db, "1", owing="900.00", oldest=2025)
        for i in range(2, 4):
            _seed(db, str(i), owing="0", oldest=None, checked=False)
        db.commit()
        with pytest.raises(ScraperExecutionError):
            await AsotinWATaxDelinquentScraper().scrape("01/01/2026", "11/12/2026")

    async def test_an_empty_cache_is_never_a_zero(self, db):
        with pytest.raises(ScraperExecutionError):
            await AsotinWATaxDelinquentScraper().scrape("01/01/2026", "11/12/2026")


def test_the_crawl_is_registered_and_scheduled():
    import src.workers.scheduler  # noqa: F401 — installs the beat schedule
    from src.workers import app

    assert "src.workers.pacs_tax_crawler.crawl_pacs_tax" in app.tasks
    assert "src.workers.pacs_tax_crawler" in app.conf.include
    assert any(v["task"] == "src.workers.pacs_tax_crawler.crawl_pacs_tax"
               for v in app.conf.beat_schedule.values())
