"""Clallam tax-foreclosure leads, against real county pages captured 2026-11-12.

- landing_2026-11-12: the Treasurer's Tax-Foreclosure-Sales page.
- certificate_2026-11-06.pdf: the newest certificate it links (8 parcels, 2 pages).
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
_TODAY = date(2026, 11, 12)  # the portal pages were captured this day
_CERT = {"url": "https://www.clallamcountywa.gov/DocumentCenter/View/30353",
         "as_of": date(2026, 11, 6), "filed": date(2026, 9, 4)}


def _gz(name: str) -> str:
    return gzip.decompress((_F / name).read_bytes()).decode("utf-8")


def _pages() -> list[str]:
    return [p.extract_text() for p in pypdf.PdfReader(_F / "certificate_2026-11-06.pdf").pages]


def _portal(pid: str = "17038") -> str:
    return _gz(f"pacs_{pid}_2026-11-12.html.gz")


def _link(href: str, text: str) -> str:
    return f'<a href="{href}">{text}</a>'


# --- certificate -----------------------------------------------------------------

def test_selects_the_newest_certificate_and_keeps_the_court_filing_date():
    url, linked, filed = select_certificate(_gz("landing_2026-11-12.html.gz"), _LANDING)
    assert url == "https://www.clallamcountywa.gov/DocumentCenter/View/30353"
    assert (linked, filed) == (date(2026, 11, 6), date(2026, 9, 4))


def test_a_page_without_a_certificate_is_a_failure_not_an_empty_list():
    with pytest.raises(ValueError, match="no dated certificate"):
        select_certificate(_link("/DocumentCenter/View/1", "Bid form"), _LANDING)


def test_an_update_date_is_never_passed_off_as_the_court_filing_date():
    html = (_link("/DocumentCenter/View/3", "Updated Certificate - 11.06.2026")
            + _link("/DocumentCenter/View/2", "2025 Certificate of Delinquency - Filed with Superior Court on 9/5/2025"))
    with pytest.raises(ValueError, match="no 2026 certificate filing link"):
        select_certificate(html, _LANDING)


def test_certificate_rows_keep_the_16_digit_parcel_and_leading_zero():
    as_of, rows = parse_certificate(_pages())
    assert as_of == date(2026, 11, 6)
    assert len(rows) == 8
    assert (rows[0]["parcel"], rows[0]["pid"], rows[0]["cert_amount"]) == ("0330061400500000", "17038", "34744.40")


@pytest.mark.parametrize("page", [0, 1])
def test_a_certificate_page_that_misses_a_parcel_fails_loud(page):
    pages = _pages()
    first_parcel = pages[page].split("ParcelNumber CertAmt LocationFullname")[1].split()[0:5]
    pages[page] = pages[page].replace(" ".join(first_parcel), " ".join(first_parcel)[:-6] + "0000", 1)
    with pytest.raises(ValueError, match=f"page {page + 1} read"):
        parse_certificate(pages)


def test_a_certificate_page_that_extracts_empty_fails_loud():
    """Codex: an empty page used to drop its rows and its 'Described as:' together."""
    with pytest.raises(ValueError, match="page 2 has no parcel list header"):
        parse_certificate([_pages()[0], ""])


# --- portal ----------------------------------------------------------------------

def test_delinquent_parcel_becomes_a_qualified_tax_lead():
    _, rows = parse_certificate(_pages())
    record, outcome = build_record(rows[0], parse_portal_status(_portal()), _CERT, cap_min_year=None, today=_TODAY)
    assert outcome == "lead"
    # The 0000 land row carries the recorder's 12-digit id; all 16 digits are kept too.
    assert record.parcel_id == "033006140050"
    assert record.enrichment_data["geographic_id"] == "0330061400500000"
    assert record.party_name == "JODI FURNISH"
    assert record.mailing_address == "153 JAMESTOWN RD, SEQUIM, WA 98382"
    assert record.property_address.startswith("153 JAMESTOWN RD")
    data = record.enrichment_data
    assert data["source"] == _SOURCE
    assert data["bill_year"] == 2017
    # 2017-2025 Total Due summed by hand from the captured page; 2026 is the current year.
    assert Decimal(data["delinquent_amount"]) == Decimal("31776.02")
    assert record.date_recorded == "09/04/2026"
    validate_tax_delinquent_records([record], "tax_delinquent")  # trusted source + fields


def test_the_product_recency_cap_applies_unless_the_source_is_exempt():
    _, rows = parse_certificate(_pages())
    assert build_record(rows[0], parse_portal_status(_portal()), _CERT, cap_min_year=2025, today=_TODAY) == (None, "capped_out")


def test_a_redeemed_parcel_is_not_a_lead():
    status = parse_portal_status(_portal("33855"))
    assert status["balances"] == {}
    row = {"parcel": "0430033490800000", "parcel_raw": "04 30 03 349080 0000", "pid": "33855",
           "cert_amount": "7672.08", "certificate_row_text": ""}
    assert build_record(row, status, _CERT, cap_min_year=None, today=_TODAY) == (None, "no_prior_year_balance")


def test_a_portal_page_for_another_parcel_is_not_delivered():
    _, rows = parse_certificate(_pages())
    other = {**rows[0], "parcel": "0330061400501000"}  # same base, different suffix
    assert build_record(other, parse_portal_status(_portal()), _CERT, cap_min_year=None, today=_TODAY) == (None, "identity_mismatch")


def test_a_portal_page_without_tax_status_fails_loud():
    with pytest.raises(ValueError, match="neither a tax-due table"):
        parse_portal_status(_portal().replace("Year - Statement ID", "Year"))


def test_an_unreadable_oldest_year_row_fails_instead_of_changing_the_bill_year():
    with pytest.raises(ValueError, match="unreadable tax-due row"):
        parse_portal_status(_portal().replace("2017 - 11328 (Balance)", "Balance"))


def test_a_statement_listed_twice_fails():
    with pytest.raises(ValueError, match="listed twice"):
        parse_portal_status(_portal().replace("2025 - 11049", "2025 - 11001"))


def test_two_statements_in_one_year_are_summed():
    balances = parse_portal_status(_portal().replace("2025 - 11049", "2024 - 11049"))["balances"]
    assert balances[2024] == Decimal("3394.77") + Decimal("3581.49")
    assert 2025 not in balances


def test_two_different_owners_are_not_resolved_by_guessing():
    html = _portal()
    second = '<tr><td>Name:</td><td>SOMEONE ELSE</td></tr>'
    html = html.replace("</table>", second + "</table>", 1)
    status = parse_portal_status(html)
    assert status["owner_conflict"] is True
    _, rows = parse_certificate(_pages())
    assert build_record(rows[0], status, _CERT, cap_min_year=None, today=_TODAY) == (None, "owner_ambiguous")


def test_only_the_0000_land_row_takes_the_12_digit_recorder_id():
    from src.scrapers.clallam_wa_tax_delinquent import lead_parcel_id

    assert lead_parcel_id("0330061400500000") == "033006140050"
    assert lead_parcel_id("0330061400501000") == "0330061400501000"   # a separate account


@pytest.mark.parametrize("today", [date(2026, 11, 16), date(2026, 11, 11)])
def test_a_stale_or_future_portal_status_fails(today):
    _, rows = parse_certificate(_pages())
    with pytest.raises(ValueError, match="is not current"):
        build_record(rows[0], parse_portal_status(_portal()), _CERT, cap_min_year=None, today=today)


# --- portal read retry (one unreadable page failed a canary, 2026-11-14) -------------

_BUSY = "<html><body>Service unavailable. Owner: JANE Q DOE, 12 ELM ST</body></html>"


def _serve(responses: list, calls: list[str]):
    """safe_get serving recorded responses in order: a page body (str) or an exception."""
    import requests

    def _get(url, **kw):
        calls.append(kw["params"]["prop_id"])
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        resp = requests.Response()
        resp.status_code, resp._content, resp.url = 200, item.encode(), url
        return resp
    return _get


def _scraper(monkeypatch, retries: int = 3):
    import src.scrapers.clallam_wa_tax_delinquent as m
    from src.config import settings
    monkeypatch.setattr(settings, "POLITE_DELAY_MS", 0)
    monkeypatch.setattr(settings, "MAX_RETRIES", retries)
    return m, m.ClallamWATaxDelinquentScraper()


def test_one_unreadable_portal_page_is_retried_and_logged_without_page_text(monkeypatch, caplog):
    import asyncio
    m, s = _scraper(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(m, "safe_get", _serve([_BUSY, _portal()], calls))
    status = asyncio.run(s._portal_status("17038", 2026))
    assert calls == ["17038", "17038"]
    assert status == parse_portal_status(_portal())
    assert "PortalPageUnreadableError" in caplog.text
    assert "JANE" not in caplog.text and "ELM" not in caplog.text


def test_a_dropped_connection_is_retried(monkeypatch):
    import asyncio

    import requests
    m, s = _scraper(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(m, "safe_get", _serve([requests.ConnectionError("reset"), _portal()], calls))
    assert asyncio.run(s._portal_status("17038", 2026)) == parse_portal_status(_portal())
    assert len(calls) == 2


def test_a_portal_page_unreadable_on_every_attempt_still_fails(monkeypatch):
    import asyncio
    m, s = _scraper(monkeypatch, retries=3)
    calls: list[str] = []
    monkeypatch.setattr(m, "safe_get", _serve([_BUSY] * 3, calls))
    with pytest.raises(m.PortalPageUnreadableError):
        asyncio.run(s._portal_status("17038", 2026))
    assert len(calls) == 3


def test_a_data_integrity_failure_is_not_retried_away(monkeypatch):
    """A statement listed twice is a real defect: a later clean page must not replace it."""
    import asyncio
    m, s = _scraper(monkeypatch)
    page = _portal()
    doubled = page.replace("2025 - 11049", "2025 - 11001")
    calls: list[str] = []
    monkeypatch.setattr(m, "safe_get", _serve([doubled, page], calls))
    with pytest.raises(ValueError, match="listed twice"):
        asyncio.run(s._portal_status("17038", 2026))
    assert len(calls) == 1
