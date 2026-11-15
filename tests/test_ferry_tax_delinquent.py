"""Ferry tax_delinquent: the tax crawl through TaxSifter and the cache-reading scraper.

Real database, real fixtures: the statewide layer's real answer for three Ferry
parcels and the real Ferry Treasurer pages (owner names, mailing, situs redacted).
The crawl's network edges are injected, as in test_asotin_tax_delinquent.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.enrichment import pacs_tax, taxsifter
from src.scrapers.enrichment.pacs import normalize_pacs_parcel
from src.scrapers.ferry_wa_tax_delinquent import FerryWATaxDelinquentScraper
from src.scrapers.reliability import ScraperExecutionError
from src.workers import pacs_tax_crawler as crawler
from src.workers.tasks_helpers.dedup import _TRUSTED_TAX_SOURCES

_FIX = Path(__file__).parent / "fixtures"
_OWING, _PAID, _UNREACHED = "33715440001000", "32916500002000", "23728900002000"
_PAGES = {_OWING: "treasurer_33715440001000_owes_current.html",
          _PAID: "treasurer_32916500002000_paid.html"}
_CLEAN = ("DELETE FROM pacs_tax_status WHERE county IN ('ferry', 'asotin')",
          "DELETE FROM external_source_health WHERE source_key = 'pacs_tax_inventory:ferry'")


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
    assert "FIPS_NR%3D%27019%27" in url  # Ferry's own scope, never another county's
    if "returnCountOnly=true" in url:
        return {"count": 3}
    return json.loads((_FIX / "wa_statewide_ferry_3_parcels.json").read_text(encoding="utf-8"))


def _resolve(county, parcel_ids, *, time_budget_s):
    out = {}
    for pid in parcel_ids:
        page = _PAGES.get(pid)
        html = (_FIX / "ferry_taxsifter" / page).read_text(encoding="utf-8") if page else None
        out[pid] = (taxsifter.parse_parcel_treasurer(html, normalize_pacs_parcel(pid)) if html
                    else pacs_tax.TaxAnswer("source_unavailable"))
    return out


def _rows(db) -> dict:
    found = db.execute(text("SELECT * FROM pacs_tax_status WHERE county = 'ferry'"))
    return {m["parcel_id"]: SimpleNamespace(**m) for m in found.mappings().all()}


def test_ferry_is_wired_to_taxsifter_but_not_scheduled_until_approved():
    assert crawler._TAX_SOURCE == {"asotin": "pacs", "ferry": "taxsifter"}
    assert "ferry" not in crawler.COUNTIES  # the first broad crawl needs owner approval


def test_the_beat_task_hands_ferry_to_the_taxsifter_resolver(monkeypatch):
    from src.scrapers.enrichment import pacs_parcel

    monkeypatch.setattr(crawler, "COUNTIES", ("asotin", "ferry"))
    used = {}
    monkeypatch.setattr(crawler, "crawl_county",
                        lambda county, db, *, fetch, resolve: used.setdefault(county, resolve)
                        and {"county": county})
    crawler.crawl_pacs_tax()
    assert used["ferry"] is taxsifter.resolve_tax
    assert used["asotin"] is pacs_parcel.resolve_tax


def test_inventory_then_one_pass(db):
    summary = crawler.crawl_county("ferry", db, fetch=_fetch, resolve=_resolve)
    assert summary["inventory_added"] == 3
    assert (summary["asked"], summary["settled"], summary["owing"]) == (2, 2, 1)
    rows = _rows(db)
    owing, paid, unreached = rows[_OWING], rows[_PAID], rows[_UNREACHED]
    assert owing.parcel_id == "33715440001000"  # stored exactly as the layer spells it
    assert owing.delinquent_amount == Decimal("679.84")
    assert owing.oldest_delinquent_year == 2026 and owing.as_of == date(2026, 11, 12)
    assert owing.owner_name == "REDACTED OWNER"
    assert owing.mailing_address == "PO BOX 100, REPUBLIC, WA 99166"
    assert paid.delinquent_amount == Decimal("0") and paid.owner_name is None
    assert unreached.attempted_at is None and unreached.checked_at is None


def test_a_truncated_inventory_read_is_an_error_not_a_smaller_county():
    def short(url):
        return {"count": 4} if "returnCountOnly=true" in url else _fetch(url)

    with pytest.raises(ValueError, match="read 3 of 4"):
        crawler.statewide_parcel_ids("ferry", short)


def _seed(db, parcel: str, *, owing: str, oldest: int | None, county="ferry", mailing=True,
          as_of: date | None = None):
    db.execute(text(
        "INSERT INTO pacs_tax_status (id, county, state, parcel_id, outcome, as_of, "
        "delinquent_amount, oldest_delinquent_year, owner_name, property_address, "
        "mailing_address, checked_at) VALUES (gen_random_uuid(), :c, 'WA', :p, "
        "'found', :asof, :amt, :old, 'REDACTED OWNER', '100 REDACTED RD, REPUBLIC 99166', "
        ":mail, :chk)"),
        {"c": county, "p": parcel, "asof": as_of or datetime.now(UTC).date(), "amt": Decimal(owing),
         "old": oldest, "mail": "PO BOX 100, REPUBLIC, WA 99166" if mailing else None,
         "chk": datetime.now(UTC)})


async def test_delivers_ferry_prior_year_debt_only(db):
    year = datetime.now(UTC).year
    _seed(db, "33600000001000", owing="900.00", oldest=year - 2)          # delivered
    _seed(db, "33600000002000", owing="679.84", oldest=year)              # current year only
    _seed(db, "33600000004000", owing="800.00", oldest=year - 2, county="asotin")  # not Ferry
    db.commit()
    records = await FerryWATaxDelinquentScraper().scrape("01/01/2026", "11/15/2026")
    assert [r.parcel_id for r in records] == ["33600000001000"]
    rec = records[0]
    assert rec.enrichment_data["source"] == "ferry_taxsifter_tax"
    assert rec.enrichment_data["bill_year"] == year - 2
    assert rec.enrichment_data["delinquent_amount"] == "900.00"


async def test_a_held_parcel_fails_the_run_instead_of_shortening_it(db):
    year = datetime.now(UTC).year
    _seed(db, "33600000001000", owing="900.00", oldest=year - 2)
    _seed(db, "33600000003000", owing="500.00", oldest=year - 1, mailing=False)  # rural: held
    db.commit()
    with pytest.raises(ScraperExecutionError, match="1 of 2 qualifying parcel"):
        await FerryWATaxDelinquentScraper().scrape("01/01/2026", "11/15/2026")


@pytest.mark.parametrize("age_days", [15, -3], ids=["stale source date", "future source date"])
async def test_a_source_date_outside_its_window_is_not_fresh(db, age_days):
    from datetime import timedelta

    _seed(db, "33600000001000", owing="900.00", oldest=datetime.now(UTC).year - 2,
          as_of=datetime.now(UTC).date() - timedelta(days=age_days))
    db.commit()
    with pytest.raises(ScraperExecutionError, match="refusing to report a partial list"):
        await FerryWATaxDelinquentScraper().scrape("01/01/2026", "11/15/2026")


async def test_an_empty_ferry_cache_is_never_a_zero(db):
    _seed(db, "33600000004000", owing="800.00", oldest=2024, county="asotin")
    db.commit()
    with pytest.raises(ScraperExecutionError):
        await FerryWATaxDelinquentScraper().scrape("01/01/2026", "11/15/2026")


def test_the_ferry_source_is_a_trusted_tax_source():
    assert "ferry_taxsifter_tax" in _TRUSTED_TAX_SOURCES


def test_the_ferry_scraper_module_is_allowlisted():
    from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES

    assert "src.scrapers.ferry_wa_tax_delinquent" in _ALLOWED_SCRAPER_MODULES
