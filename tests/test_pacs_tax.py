"""PACS tax statements -> past-due amount, on REAL Asotin pages (names redacted)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from src.scrapers.enrichment import pacs_tax as pt

_FIX = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


def test_a_parcel_owing_this_years_tax_after_both_due_dates_is_delinquent():
    # Parcel 10014003000000000 (prop 1651): nothing paid on 2026, interest accruing.
    a = pt.parse_tax(_page("asotin_pacs_detail_1651.html"), "10014003000000000")
    assert a.outcome == pt.FOUND and a.as_of == date(2026, 11, 12)
    assert [s.year for s in a.statements] == [2026, 2025, 2024]
    assert a.delinquent_amount == Decimal("2270.78")
    assert a.oldest_delinquent_year == 2026
    assert a.past_due[0].interest == Decimal("113.07")


def test_a_paid_up_parcel_owes_nothing():
    a = pt.parse_tax(_page("asotin_pacs_detail_2712.html"), "10040300100010000")
    assert a.outcome == pt.FOUND and len(a.statements) == 3
    assert a.delinquent_amount == Decimal("0") and a.oldest_delinquent_year is None


def test_jurisdiction_rows_are_never_counted_as_statements():
    # The paid-up page nests a TOTAL + per-jurisdiction rows under each statement.
    a = pt.parse_tax(_page("asotin_pacs_detail_2712.html"), "10040300100010000")
    assert {s.statement_id for s in a.statements} == {"1566", "1572", "27122024"}


def test_a_page_for_another_parcel_is_a_mismatch_not_a_balance():
    assert pt.parse_tax(_page("asotin_pacs_detail_1651.html"), "1001400300000").outcome == pt.PARCEL_MISMATCH


def test_a_page_without_the_tax_table_is_unknown_not_zero():
    html = _page("asotin_pacs_detail_1651.html").replace("ctl00_details_detailsTable", "somethingElse")
    assert pt.parse_tax(html, "10014003000000000").outcome == pt.UNPARSED


def test_before_the_first_due_date_nothing_is_past_due():
    owing = pt.Statement(2026, "1", Decimal("100.00"), Decimal("0"), Decimal("0"))
    assert pt.TaxAnswer(pt.FOUND, as_of=date(2026, 4, 30), statements=[owing]).past_due == []
    assert pt.TaxAnswer(pt.FOUND, as_of=date(2026, 5, 1), statements=[owing]).past_due == [owing]


def test_a_credit_balance_is_not_a_debt():
    credit = pt.Statement(2026, "1", Decimal("-689.64"), Decimal("0"), Decimal("0"))
    assert pt.TaxAnswer(pt.FOUND, as_of=date(2026, 11, 12), statements=[credit]).delinquent_amount == 0


def test_the_lead_identity_comes_off_the_same_page():
    a = pt.parse_tax(_page("asotin_pacs_detail_2712.html"), "10040300100010000")
    assert a.owner_name == "OWNER REDACTED"  # fixture names are redacted
    assert a.mailing_address == "831 18TH AVE, CLARKSTON, WA 99403"
    assert a.property_address and a.property_address.endswith("CLARKSTON, WA 99403")
