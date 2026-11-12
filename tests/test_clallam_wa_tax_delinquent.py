"""Clallam tax-foreclosure leads, against real county pages captured 2026-11-12.

- landing_2026-11-12: the Treasurer's Tax-Foreclosure-Sales page.
- certificate_2026-11-06.pdf: the newest certificate it links (8 parcels).
- pacs_17038: a parcel on that certificate, owing 2017-2026.
- pacs_33855: a parcel on the 9/4 certificate but not the 11/6 one ("No Amount Due").
"""
import gzip
from datetime import date
from decimal import Decimal
from pathlib import Path

import pypdf
import pytest

from src.scrapers.clallam_wa_tax_delinquent import (
    _SOURCE,
    build_record,
    parse_certificate,
    parse_portal_status,
    select_certificate,
)
from src.workers.tasks_helpers.dedup import validate_tax_delinquent_records

_F = Path(__file__).parent / "fixtures" / "clallam_tax"
_LANDING = "https://www.clallamcountywa.gov/1077/Tax-Foreclosure-Sales"


def _gz(name: str) -> str:
    return gzip.decompress((_F / name).read_bytes()).decode("utf-8")


def _cert_text() -> str:
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(_F / "certificate_2026-11-06.pdf").pages)


_CERT = {"url": "https://www.clallamcountywa.gov/DocumentCenter/View/30353",
         "as_of": date(2026, 11, 6), "filed": date(2026, 9, 4)}


def test_selects_the_newest_certificate_and_keeps_the_court_filing_date():
    url, linked, filed = select_certificate(_gz("landing_2026-11-12.html.gz"), _LANDING)
    assert url == "https://www.clallamcountywa.gov/DocumentCenter/View/30353"
    assert (linked, filed) == (date(2026, 11, 6), date(2026, 9, 4))


def test_a_page_without_a_certificate_is_a_failure_not_an_empty_list():
    with pytest.raises(ValueError, match="no dated certificate"):
        select_certificate("<html><a href='/DocumentCenter/View/1'>Bid form</a></html>", _LANDING)


def test_certificate_rows_keep_the_16_digit_parcel_and_leading_zero():
    as_of, rows = parse_certificate(_cert_text())
    assert as_of == date(2026, 11, 6)
    assert len(rows) == 8
    first = rows[0]
    assert (first["parcel"], first["pid"], first["cert_amount"]) == ("0330061400500000", "17038", "34744.40")


def test_a_certificate_read_that_misses_a_parcel_fails_loud():
    text = _cert_text().replace("13 32 10 510020 0000", "13 32 10 51002 0000")  # one row no longer parses
    assert text != _cert_text()
    with pytest.raises(ValueError, match="read 7 parcels of 8"):
        parse_certificate(text)


def test_delinquent_parcel_becomes_a_qualified_tax_lead():
    _, rows = parse_certificate(_cert_text())
    status = parse_portal_status(_gz("pacs_17038_2026-11-12.html.gz"))
    record, outcome = build_record(rows[0], status, _CERT, cap_min_year=None)
    assert outcome == "lead"
    assert record.parcel_id == "0330061400500000"
    assert record.party_name and record.property_address and record.mailing_address
    data = record.enrichment_data
    assert data["source"] == _SOURCE
    assert data["bill_year"] == 2017
    # 2017-2025 Total Due summed by hand from the captured page; 2026 is the current year.
    assert Decimal(data["delinquent_amount"]) == Decimal("31776.02")
    assert record.date_recorded == "09/04/2026"
    validate_tax_delinquent_records([record], "tax_delinquent")  # trusted source + fields


def test_the_product_recency_cap_applies_unless_the_source_is_exempt():
    _, rows = parse_certificate(_cert_text())
    status = parse_portal_status(_gz("pacs_17038_2026-11-12.html.gz"))
    assert build_record(rows[0], status, _CERT, cap_min_year=2025) == (None, "capped_out")


def test_a_redeemed_parcel_is_not_a_lead():
    status = parse_portal_status(_gz("pacs_33855_2026-11-12.html.gz"))
    assert status["balances"] == {}
    row = {"parcel": "0430033490800000", "parcel_raw": "04 30 03 349080 0000", "pid": "33855",
           "cert_amount": "7672.08", "cert_situs": ""}
    assert build_record(row, status, _CERT, cap_min_year=None) == (None, "no_prior_year_balance")


def test_a_portal_page_for_another_parcel_is_not_delivered():
    _, rows = parse_certificate(_cert_text())
    status = parse_portal_status(_gz("pacs_17038_2026-11-12.html.gz"))
    other = {**rows[0], "parcel": "0330061400501000"}  # same base, different suffix
    assert build_record(other, status, _CERT, cap_min_year=None) == (None, "identity_mismatch")


def test_a_portal_page_without_tax_status_fails_loud():
    html = _gz("pacs_17038_2026-11-12.html.gz").replace("Year - Statement ID", "Year")
    with pytest.raises(ValueError, match="neither a tax-due table"):
        parse_portal_status(html)
