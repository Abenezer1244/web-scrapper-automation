"""Benton tax foreclosure: the Treasurer's listing joined with each parcel's live account.

Fixtures are the real pages, captured 2026-11-12: the Treasurer's Foreclosure and
Distraint page, its 2026 listing PDF (3 parcels, sale 11/16 + 11/19 on GovEase),
and the county PACS page of each listed parcel. benton_pacs_detail_58808.html is a
real paid-up account (statements at $0.00). Account pages are read by the shared
pacs_tax parser (Asotin), the same reader every PACS county uses.
"""
import gzip
from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.benton_wa_tax_delinquent import (
    SOURCE,
    build_records,
    find_listing_url,
    parse_listing,
)
from src.scrapers.enrichment import pacs_tax
from src.scrapers.enrichment.pacs_parcel import normalize_pacs_parcel
from src.scrapers.enrichment.pacs_tax import TaxAnswer, parse_tax
from src.scrapers.sources.nts_pdf import extract_pdf_text
from src.workers.tasks_helpers.dedup import _extract_tax_fields, validate_tax_delinquent_records

_DIR = Path(__file__).parent / "fixtures" / "benton_tax"
_PARCELS = ("102892110004002", "110982020264019", "131994020005038")


def _listing_text() -> str:
    return extract_pdf_text((_DIR / "foreclosure_listing_2026.pdf").read_bytes())


def _answers() -> dict:
    return {
        p: parse_tax(gzip.decompress((_DIR / f"pacs_{p}.html.gz").read_bytes()).decode("utf-8"),
                            normalize_pacs_parcel(p))
        for p in _PARCELS
    }


def test_listing_parses_and_reconciles_with_its_own_header():
    listing = parse_listing(_listing_text())
    assert [p.parcel for p in listing.parcels] == list(_PARCELS)
    assert [p.minimum_bid for p in listing.parcels] == [Decimal("11292.00"), Decimal("11612.00"), Decimal("17214.00")]
    assert listing.total == Decimal("40118.00")
    assert listing.sale_dates == ["11/16/2026", "11/19/2026"]
    assert listing.sale_venue == "www.govease.com"
    assert listing.valid_through == "11/13/2026"


def test_a_listing_that_does_not_add_up_is_refused():
    text = _listing_text().replace("3 PARCELS IN TAX FORECLOSURE", "4 PARCELS IN TAX FORECLOSURE")
    with pytest.raises(ValueError, match="lists 4 parcels but 3 rows parsed"):
        parse_listing(text)


def test_the_listing_link_resolves_against_the_site_root():
    html = gzip.decompress((_DIR / "treasurer_page.html.gz").read_bytes()).decode("utf-8")
    url = find_listing_url(html)
    assert url.startswith("https://www.bentoncountywa.gov/Documents/Government/Elected%20Officials/"
                          "Treasurer/Foreclosure%20and%20Distraint/Current%20Listings/2026%20Web%20Posting")
    assert "Notice%20of%20Sale" not in url


def test_a_page_whose_listing_link_moved_fails_loud_rather_than_reading_as_empty():
    html = gzip.decompress((_DIR / "treasurer_page.html.gz").read_bytes()).decode("utf-8")
    with pytest.raises(RuntimeError, match="links no current foreclosure listing"):
        find_listing_url(html.replace("Current Listings/", "Archive/"))


def _page(parcel: str) -> str:
    return gzip.decompress((_DIR / f"pacs_{parcel}.html.gz").read_bytes()).decode("utf-8")


def _build_one(parcel: str, answer) -> dict:
    records, skipped = build_records(parse_listing(_listing_text()), {**_answers(), parcel: answer}, "u")
    return skipped


def test_a_statement_table_with_unrecognised_rows_is_unread_not_paid():
    """Codex: changed row markup must not read as a paid account."""
    html = _page("102892110004002").replace('class="rowEnd"', 'class="row"')
    answer = parse_tax(html, normalize_pacs_parcel("102892110004002"))
    assert answer.statements == []
    assert _build_one("102892110004002", answer)["unresolved"] == 1


def test_a_page_without_an_owner_block_is_unread_not_a_mismatch():
    html = _page("102892110004002").replace("<td>Name:</td>", "<td>Holder:</td>")
    answer = parse_tax(html, normalize_pacs_parcel("102892110004002"))
    assert answer.owner_name is None
    assert _build_one("102892110004002", answer)["unresolved"] == 1


def test_account_page_gives_statements_owner_and_mailing():
    a = _answers()["102892110004002"]
    assert a.outcome == pacs_tax.FOUND and a.as_of.isoformat() == "2026-11-12"
    assert {(s.year, s.amount_due) for s in a.statements} == {
        (2026, Decimal("2319.94")), (2025, Decimal("2664.67")),
        (2024, Decimal("2584.41")), (2023, Decimal("5031.34"))}
    assert a.owner_name == "NELSON SHIRLEY L"
    assert a.mailing_address == "2405 W KENNEWICK AVE, KENNEWICK, WA 99336"


def test_a_page_for_another_parcel_is_never_used():
    html = _page("102892110004002")
    assert parse_tax(html, normalize_pacs_parcel("110982020264019")).outcome == pacs_tax.PARCEL_MISMATCH


def test_records_carry_past_due_debt_oldest_year_and_the_sale():
    records, skipped = build_records(parse_listing(_listing_text()), _answers(), "https://x/listing.pdf")
    assert skipped == {"paid_or_redeemed": 0, "owner_not_verified": 0, "unresolved": 0}
    by_parcel = {r.parcel_id: r for r in records}
    r = by_parcel["102892110004002"]
    assert r.party_name == "NELSON SHIRLEY L"
    assert r.mailing_address == "2405 W KENNEWICK AVE, KENNEWICK, WA 99336"
    e = r.enrichment_data
    # Past due per RCW 84.56.020 (pacs_tax, as every PACS county): 2023-2026 on 11/12/2026.
    assert e["delinquent_amount"] == "12600.36"
    assert e["bill_year"] == 2023 and e["delinquent_years"] == [2023, 2024, 2025, 2026]
    assert e["as_of_date"] == "11/12/2026"
    assert e["foreclosure_minimum_bid"] == "11292.00"
    assert e["foreclosure_sale_dates"] == ["11/16/2026", "11/19/2026"]
    assert by_parcel["131994020005038"].party_name == "HELGESON GERALD J & VICKIE L"


def test_a_paid_or_redeemed_account_is_not_a_lead():
    paid_html = (Path(__file__).parent / "fixtures" / "benton_pacs_detail_58808.html").read_text(encoding="utf-8")
    paid = parse_tax(paid_html, normalize_pacs_parcel("131073011125003"))
    assert paid.outcome == pacs_tax.FOUND and paid.statements and not paid.past_due
    assert _build_one("102892110004002", paid)["paid_or_redeemed"] == 1


def test_an_owner_the_listing_does_not_name_is_never_delivered():
    """Sold or transferred since the listing: the live owner is not the taxpayer printed."""
    a = _answers()["110982020264019"]
    moved = TaxAnswer(pacs_tax.FOUND, as_of=a.as_of, statements=a.statements,
                      owner_name="A DIFFERENT OWNER", property_address=a.property_address,
                      mailing_address=a.mailing_address)
    assert _build_one("110982020264019", moved)["owner_not_verified"] == 1


def test_records_pass_the_tax_invariant_once_the_source_is_trusted():
    records, _ = build_records(parse_listing(_listing_text()), _answers(), "u")
    validate_tax_delinquent_records(records, "tax_delinquent")  # raises on an untrusted source
    assert all(_extract_tax_fields(r.enrichment_data, "tax_delinquent")[1] is not None for r in records)
    assert {r.enrichment_data["source"] for r in records} == {SOURCE}
