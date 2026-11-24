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
    # The minimum bid is not a balance owed: it is never stored as one.
    assert (ed["source"], ed["foreclosure_minimum_bid"]) == ("cowlitz_county_tax_foreclosure", "22912.46")
    assert "delinquent_amount" not in ed and "delinquent_amount_basis" not in ed
    assert ed["bill_year"] is None                       # not published per parcel: never guessed
    assert ed["foreclosure_sale_dates"] == ["12/07/2026"] and ed["as_of_date"] == "11/05/2026"
    assert r["080980081"].enrichment_data["foreclosure_status"] == "tax_foreclosure_sale_date_pending"
    assert ed["foreclosure_status"] == "tax_foreclosure_sale_tentative"   # the county's own word


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


def test_migration_168_seeds_an_inactive_connector_at_this_scraper():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "168_add_cowlitz_tax_delinquent.py"
    spec = importlib.util.spec_from_file_location("_mig168", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("168", "167")
    module_path, cls = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert getattr(importlib.import_module(module_path), cls) is ctd.CowlitzWATaxDelinquentScraper
    assert "'unknown',\n            false" in path.read_text(encoding="utf-8")


# ── the minimum bid is never a balance owed (2026-11-17) ──────────────────────────

def test_a_cowlitz_row_has_no_balance_even_if_its_enrichment_carries_one():
    from src.workers.tasks_helpers.dedup import _extract_tax_fields

    stale = {"source": "cowlitz_county_tax_foreclosure", "delinquent_amount": "22912.46",
             "foreclosure_minimum_bid": "22912.46", "bill_year": None}
    assert _extract_tax_fields(stale, "tax_delinquent") == (None, None)
    other = {**stale, "source": "benton_county_tax_foreclosure", "bill_year": 2022}
    assert _extract_tax_fields(other, "tax_delinquent") == (Decimal("22912.46"), 2022)


@pytest.mark.parametrize("bid", [None, "", "not money", "-5", "123456789012", "0", "0.001"],
                         ids=["missing", "blank", "text", "negative", "too-large", "zero", "rounds-to-zero"])
def test_a_cowlitz_row_without_a_clean_bid_fails_the_job(bid):
    from src.workers.tasks_helpers.dedup import (
        TaxDelinquentInvariantError,
        validate_tax_delinquent_records,
    )

    records, _ = ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 11, 15), _HOLDERS)
    records[0].enrichment_data = {**records[0].enrichment_data, "foreclosure_minimum_bid": bid}
    with pytest.raises(TaxDelinquentInvariantError, match="delinquent_amount=None"):
        validate_tax_delinquent_records(records, "tax_delinquent")


def test_the_export_puts_the_bid_in_its_own_column_never_under_balance_owed():
    from src.utils.lead_export import (
        CRM_V1_LABELS,
        build_lead_export_row,
        resolve_crm_export_columns,
        resolve_lead_export_columns,
    )

    records, _ = ctd.build_records(_CYCLE, ctd.parse_listing(_TEXT), date(2026, 11, 15), _HOLDERS)
    rec = next(r for r in records if r.parcel_id == "WL2408005")
    row = build_lead_export_row({"record_type": "tax_delinquent", "parcel_id": rec.parcel_id,
                                 "delinquent_amount": None, "enrichment_data": rec.enrichment_data})
    assert (row["delinquent_amount"], row["foreclosure_minimum_bid"]) == ("", "22912.46")
    assert "foreclosure_minimum_bid" in resolve_lead_export_columns("tax_delinquent")
    assert "foreclosure_minimum_bid" not in resolve_lead_export_columns("probate")
    crm = resolve_crm_export_columns("tax_delinquent")
    assert crm[-1] == "foreclosure_minimum_bid" and CRM_V1_LABELS[crm[-1]] == "Foreclosure Minimum Bid"
    # Another record type never shows a bid, even if its enrichment happens to carry one.
    probate = build_lead_export_row({"record_type": "probate", "enrichment_data": {"foreclosure_minimum_bid": "1"}})
    assert probate["foreclosure_minimum_bid"] == ""
    # A SQL-fed row (segments, batch) passes the projected value instead of enrichment.
    projected = build_lead_export_row({"record_type": "tax_delinquent", "foreclosure_minimum_bid": "5000"})
    assert projected["foreclosure_minimum_bid"] == "5000.00"


@pytest.mark.parametrize("line,bid", [
    ("WL2408005 3033148 SMITH JOHN 123 MAIN ST 0.25 100,000 22,912.46", "22,912.46"),
    ("WL2408005 3033148 SMITH JOHN 123 MAIN ST 0.25 100,000 -5.00", None),
    ("WL2408005 3033148 SMITH JOHN 123 MAIN ST 0.25 100,000 x5.00", None),
], ids=["bid", "negative", "glued"])
def test_the_bid_is_the_whole_last_token(line, bid):
    m = ctd._BID_RE.search(line)
    assert (m.group(1) if m else None) == bid


def test_a_reused_amount_is_never_copied_onto_a_bid_only_row():
    import inspect

    from src.workers.tasks_helpers import enrich

    src = inspect.getsource(enrich)
    assert "rn.enrichment_data->>'source' IN ({_bid_only})" in src
    assert "_BID_ONLY_TAX_SOURCES" in src
