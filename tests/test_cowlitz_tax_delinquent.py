"""Cowlitz tax_delinquent: the Treasurer's current tax-foreclosure listing (Phase 4).

Fixture: the live listing text, "Last Update 11/5/2026, Count = 7", cause 26-2-00582-08,
owner names redacted (live 2026-11-15). The 2026 cycle page says: "The tentative auction
date for all parcels, except for parcel number 080980081, is December 7, 2026."
"""
import importlib
import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES
from src.scrapers import cowlitz_wa_tax_delinquent as ctd
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.workers.tasks_helpers.dedup import _TRUSTED_TAX_SOURCES

_TEXT = (Path(__file__).parent / "fixtures" / "cowlitz_tax_foreclosure_2026-11-05.txt").read_text(encoding="utf-8")
_PAGE = ("<p>The tentative auction date for all parcels, except for parcel number 080980081, is "
         "<strong>December 7, 2026</strong>.</p>"
         '<a href="/DocumentCenter/View/38707/2026-Foreclosure-Property-Information">here</a>')
_CYCLE = ctd.parse_cycle(_PAGE, 2026, "https://www.co.cowlitz.wa.us/3324/2026-Foreclosure")
# The Assessor layer's DEED_HOLDER_NAME for each listed parcel (as the live layer answered
# 2026-11-15, names redacted the same way as the fixture).
_HOLDERS = {"6204902": "SAMPLE OWNER1", "WL2408005": "SAMPLE OWNER2 ETAL", "09531": "SAMPLE OWNER3",
            "080980081": "SAMPLE OWNER4", "2417301": "LOWER COLUMBIA COMMUNITY ACTION",
            "WK0412001": "SAMPLE OWNER5", "EF2102003": "SAMPLE OWNER6"}


def test_the_listing_reconciles_with_its_count_and_title_reports():
    listing = ctd.parse_listing(_TEXT)
    assert listing.last_update == date(2026, 11, 5) and listing.cause_number == "26-2-00582-08"
    assert [p.parcel for p in listing.parcels] == [
        "6204902", "WL2408005", "09531", "080980081", "2417301", "WK0412001", "EF2102003"]
    by = {p.parcel: p for p in listing.parcels}
    assert by["09531"].property_id == "3033148"                       # leading zero kept
    assert by["WL2408005"].holder == "SAMPLE OWNER2 ETAL"               # name over two lines
    assert by["WL2408005"].situs == "5228 OCEAN BEACH HWY"
    assert by["09531"].holder == "SAMPLE OWNER3" and by["09531"].situs == "708 WASHINGTON WAY"
    assert by["6204902"].situs is None                                  # "Bare Land"
    assert by["2417301"].holder == "LOWER COLUMBIA COMMUNITY ACTION"
    assert by["WK0412001"].minimum_bid == Decimal("2186.16")
    assert [str(p.minimum_bid) for p in listing.parcels] == [   # as printed, row by row
        "4388.20", "22912.46", "15862.77", "13305.56", "4117.49", "2186.16", "2045.79"]


def test_a_listing_short_of_its_count_fails():
    with pytest.raises(ValueError, match="states 8 parcels"):
        ctd.parse_listing(_TEXT.replace("Count = 7", "Count = 8"))


def test_a_listing_without_its_header_fails():
    with pytest.raises(ValueError, match="no 'Last Update'"):
        ctd.parse_listing(_TEXT.replace("Last Update", "Updated"))


def test_the_cycle_page_gives_the_sale_and_the_excepted_parcel():
    assert _CYCLE.sale_date == date(2026, 12, 7)
    assert _CYCLE.excepted == {"080980081"}
    assert _CYCLE.listing_url.endswith("/DocumentCenter/View/38707/2026-Foreclosure-Property-Information")


def test_a_cycle_page_without_a_sale_date_fails():
    with pytest.raises(RuntimeError, match="no auction date"):
        ctd.parse_cycle(_PAGE.replace("auction date", "date"), 2026, "https://www.co.cowlitz.wa.us/x")


def test_the_newest_cycle_is_found():
    html = '<a href="/3183/2025-Foreclosure">2025</a><a href="/3324/2026-Foreclosure">2026</a>'
    assert ctd.newest_cycle_url(html) == (2026, "https://www.co.cowlitz.wa.us/3324/2026-Foreclosure")


def test_before_the_sale_every_listed_parcel_is_a_lead():
    records, _skipped = ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 11, 15), _HOLDERS)
    assert len(records) == 7
    r = {x.parcel_id: x for x in records}
    ed = r["WL2408005"].enrichment_data
    assert (ed["source"], ed["delinquent_amount"], ed["delinquent_amount_basis"]) == (
        "cowlitz_county_tax_foreclosure", "22912.46", "foreclosure_minimum_bid")
    assert ed["bill_year"] is None                       # not published per parcel: never guessed
    assert ed["foreclosure_sale_dates"] == ["12/07/2026"] and ed["as_of_date"] == "11/05/2026"
    assert r["080980081"].enrichment_data["foreclosure_status"] == "tax_foreclosure_sale_date_pending"


def test_after_a_tentative_sale_date_the_run_fails_instead_of_a_quiet_zero():
    with pytest.raises(RuntimeError, match="has passed but 6 parcels are still listed"):
        ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 12, 8), _HOLDERS)


def test_a_future_dated_or_other_cycle_listing_is_refused():
    listing = ctd.parse_listing(_TEXT)
    with pytest.raises(RuntimeError, match="future-dated"):
        ctd.build_records(_CYCLE, listing, date(2026, 11, 1), _HOLDERS)
    next_cycle = ctd.Cycle(year=2027, sale_date=date(2027, 12, 6), listing_url="x")
    with pytest.raises(RuntimeError, match="is for 2026, the page for 2027"):
        ctd.build_records(next_cycle, listing, date(2026, 11, 15), _HOLDERS)


def test_a_holder_the_assessor_does_not_confirm_fails_the_run():
    """A number inside an entity name ("ABC 123 HOLDINGS LLC") must not silently split
    into owner "ABC" and address "123 HOLDINGS LLC" (Codex P1)."""
    text = _TEXT.replace("SAMPLE OWNER3 708 WASHINGTON WAY", "ABC 123 HOLDINGS LLC")
    listing = ctd.parse_listing(text)
    assert {p.parcel: p.holder for p in listing.parcels}["09531"] == "ABC"   # the bad split
    holders = {**_HOLDERS, "09531": "ABC 123 HOLDINGS LLC"}
    with pytest.raises(RuntimeError, match="not confirmed .* 09531"):
        ctd.build_records(_CYCLE, listing, date(2026, 11, 15), holders)


def test_a_duplicate_title_report_link_fails():
    dup = "3033148: https://apps.co.cowlitz.wa.us/Foreclosures/2026/3033148_09531.pdf"
    with pytest.raises(ValueError, match="appears twice"):
        ctd.parse_listing(_TEXT + "\n" + dup.replace("3033148", "3099999", 1).replace("3033148_", "3099999_"))


def test_the_rows_pass_the_real_tax_validator_without_a_year():
    from src.workers.tasks_helpers.dedup import (
        TaxDelinquentInvariantError,
        validate_tax_delinquent_records,
    )

    records, _ = ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 11, 15), _HOLDERS)
    validate_tax_delinquent_records(records, "tax_delinquent")   # no raise: year not published
    records[0].enrichment_data = {**records[0].enrichment_data, "source": "benton_county_tax_foreclosure"}
    with pytest.raises(TaxDelinquentInvariantError, match="bill_year=None"):
        validate_tax_delinquent_records(records, "tax_delinquent")   # other sources still need one


def test_a_stale_listing_is_never_read_as_current():
    with pytest.raises(RuntimeError, match="too old"):
        ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 12, 21), _HOLDERS)   # 46 days


def test_the_source_is_trusted_cap_exempt_and_allowlisted():
    assert ctd.SOURCE in _TRUSTED_TAX_SOURCES and ctd.SOURCE in TAX_CAP_EXEMPT_SOURCES
    assert "src.scrapers.cowlitz_wa_tax_delinquent" in _ALLOWED_SCRAPER_MODULES


def test_migration_164_seeds_an_inactive_connector_at_this_scraper():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "164_add_cowlitz_tax_delinquent.py"
    spec = importlib.util.spec_from_file_location("_mig164", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("164", "163")
    module_path, cls = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert getattr(importlib.import_module(module_path), cls) is ctd.CowlitzWATaxDelinquentScraper
    assert "'unknown',\n            false" in path.read_text(encoding="utf-8")
