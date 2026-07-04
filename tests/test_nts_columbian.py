"""Ingestion + parse tests for the Clark County (The Columbian) NTS adapter.

Runs against REAL saved pages (tests/fixtures/nts_columbian_clark_listing.html and
…_ad_detail.html — Clark County legal notices, fetched 2026-07-04), per the no-mocks
rule. Pins: the listing→permalink extraction, the ad-body extraction, and the full
parse of a real Clark MTC notice through the SHARED parser (incl. the dual-label
Original/Current trustee + beneficiary-bleed fix).
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.scrapers.sources.nts_columbian import (
    BASE_URL,
    COUNTY,
    SOURCE,
    extract_ad_body,
    extract_ad_detail_urls,
)
from src.scrapers.sources.nts_tacoma_index import (
    is_valid_nts,
    notice_to_row,
    parse_tacoma_notice,
)

_FIX = Path(__file__).parent / "fixtures"
_LISTING = (_FIX / "nts_columbian_clark_listing.html").read_text(encoding="utf-8")
_AD_DETAIL = (_FIX / "nts_columbian_clark_ad_detail.html").read_text(encoding="utf-8")
# The real trustee-sale ad on the fixture listing (TS No WA07000393-24-1).
_TRUSTEE_AD = f"{BASE_URL}/ad-details/61259438"


class TestExtractAdDetailUrls:
    def test_pulls_all_permalinks_host_pinned(self):
        urls = extract_ad_detail_urls(_LISTING)
        # ~32 mixed legal notices on the rolling listing; every permalink is fetched
        # (no preview pre-filter) so a trustee sale can never be silently skipped.
        assert len(urls) >= 30
        assert all(u.startswith(f"{BASE_URL}/ad-details/") for u in urls)
        assert all(u.rsplit("/", 1)[-1].isdigit() for u in urls)

    def test_includes_the_real_trustee_ad(self):
        assert _TRUSTEE_AD in extract_ad_detail_urls(_LISTING)

    def test_order_preserving_dedupe(self):
        urls = extract_ad_detail_urls(_LISTING)
        assert len(urls) == len(set(urls))

    def test_rejects_offsite_ad_details_link(self):
        # a syndicated/compromised absolute /ad-details link on another host must NOT
        # be crawled (defense-in-depth with the crawler's same_origin_as)
        html = (
            '<a class="view_post" href="https://evil.example.com/ad-details/999">x</a>'
            '<a class="view_post" href="/ad-details/12345">ok</a>'
        )
        urls = extract_ad_detail_urls(html)
        assert urls == [f"{BASE_URL}/ad-details/12345"]

    def test_ignores_non_ad_links(self):
        html = (
            '<a href="/all-classifieds">All</a>'
            '<a href="https://www.facebook.com/x">fb</a>'
            '<a class="view_post" href="/ad-details/42">ad</a>'
        )
        assert extract_ad_detail_urls(html) == [f"{BASE_URL}/ad-details/42"]


class TestExtractAdBody:
    def test_extracts_notice_text_from_container(self):
        body = extract_ad_body(_AD_DETAIL)
        assert "NOTICE OF TRUSTEES SALE" in body
        assert "KENNY D OLSON" in body
        # chrome/scripts stripped
        assert "googletag" not in body

    def test_empty_when_no_container(self):
        assert extract_ad_body("<html><body><p>no ad here</p></body></html>") == ""


class TestParseRealClarkNotice:
    """The real Clark MTC notice — a month-name auction date + the dual-label
    'Original Trustee … Current Trustee …' layout that the shared-parser fix targets."""

    def setup_method(self):
        self.p = parse_tacoma_notice(extract_ad_body(_AD_DETAIL))

    def test_ts_number(self):
        assert self.p["ts_number"] == "WA07000393-24-1"

    def test_auction_date_month_name(self):
        assert self.p["auction_date"] == "July 17, 2026"

    def test_current_trustee_not_original(self):
        # THE fix: the acting trustee is MTC (Current), not FIDELITY (Original).
        assert self.p["trustee"] == "MTC Financial Inc. dba Trustee Corps"

    def test_beneficiary_does_not_bleed_into_original_trustee(self):
        assert self.p["beneficiary"] == (
            "Idaho Housing and Finance Association (which also dba HomeLoanServ)"
        )
        assert "Original Trustee" not in self.p["beneficiary"]
        assert "FIDELITY" not in self.p["beneficiary"]

    def test_property_and_parcel(self):
        assert self.p["property_address"] == "1408 NE 125TH AVE, VANCOUVER, WA 98684"
        assert self.p["parcel"] == "110170068"

    def test_principal_owing(self):
        assert self.p["principal_owing"] == Decimal("440867.00")

    def test_is_valid(self):
        assert is_valid_nts(self.p) is True


class TestNoticeToRowClark:
    def test_row_carries_clark_source_and_county(self):
        p = parse_tacoma_notice(extract_ad_body(_AD_DETAIL))
        row = notice_to_row(p, _TRUSTEE_AD, today=date(2026, 7, 4), source=SOURCE, county=COUNTY)
        assert row is not None
        assert row["source"] == "columbian_classifieds"
        assert row["county"] == "clark" and row["state"] == "WA"
        assert row["auction_date"] == date(2026, 7, 17)
        assert row["is_active"] is True
        assert row["trustee"] == "MTC Financial Inc. dba Trustee Corps"
        assert len(row["raw_hash"]) == 64
