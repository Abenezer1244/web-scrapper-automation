"""Parser tests for the Tacoma Daily Index NTS parser.

Runs against a REAL saved notice (tests/fixtures/nts_tacoma_25-76127.txt — Pierce
County NTS, fetched 2026-06-12), per the no-mocks rule. Pins the investor-critical
field extraction.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.scrapers.sources.nts_tacoma_index import (
    extract_article_text,
    extract_notice_urls,
    is_valid_nts,
    notice_to_row,
    parse_nts_notice,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "nts_tacoma_25-76127.txt"


def _load() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


class TestParseRealNotice:
    def setup_method(self):
        self.p = parse_nts_notice(_load())

    def test_ts_number(self):
        assert self.p["ts_number"] == "25-76127"

    def test_auction_date_time_location(self):
        assert self.p["auction_date"] == "7/10/2026"
        assert self.p["auction_time"] == "10:00 AM"
        assert "Pierce County Courthouse" in self.p["auction_location"]

    def test_parties(self):
        assert "SPURLING" in self.p["grantor"]
        assert self.p["trustee"] == "North Star Trustee, LLC"
        assert self.p["beneficiary"] == "Freedom Mortgage Corporation"

    def test_parcel(self):
        assert self.p["parcel"] == "051928-5029"

    def test_property_address(self):
        addr = self.p["property_address"]
        assert "19012 160TH ST EAST" in addr
        assert "BONNEY LAKE" in addr
        assert "98391" in addr
        assert "WA" in addr  # WASHINGTON normalized to WA

    def test_default_and_note_amounts(self):
        assert self.p["principal_owing"] == Decimal("185895.06")  # section IV sum owing
        assert self.p["note_amount"] == Decimal("234533.00")      # original loan

    def test_deed_reference_and_nod_date(self):
        assert self.p["deed_reference"] == "200907160286"
        assert self.p["nod_date"] == "1/20/2026"

    def test_is_valid(self):
        assert is_valid_nts(self.p) is True


class TestValidationGuards:
    def test_empty_text_invalid(self):
        assert is_valid_nts(parse_nts_notice("")) is False

    def test_site_chrome_invalid(self):
        chrome = "Home News Legal Notices Subscribe Weather Contact About Us"
        assert is_valid_nts(parse_nts_notice(chrome)) is False

    def test_missing_auction_date_invalid(self):
        # has a TS# but no auction clause -> not usable
        assert is_valid_nts(parse_nts_notice("TS #: 99-00000\nGrantor: X")) is False

    def test_curly_apostrophe_normalized(self):
        # CMS emits curly quotes in "TRUSTEE’S SALE"; must not break labels
        body = "TS #: 25-0001\nwill on 8/1/2026, at 9:00 AM at Courthouse sell at public auction"
        p = parse_nts_notice(body)
        assert p["ts_number"] == "25-0001" and p["auction_date"] == "8/1/2026"

    def test_money_parsing_handles_commas(self):
        body = "Note Amount: $1,234,567.89"
        assert parse_nts_notice(body)["note_amount"] == Decimal("1234567.89")


class TestTrusteeFormatVariety:
    """Codex review: parser must survive the layout variance across WA trustees."""

    def test_ts_number_prefixed_format(self):
        # Quality Loan / Clear Recon style prefixed sale numbers
        for raw, want in (
            ("TS #: WA-25-123456", "WA-25-123456"),
            ("T.S. No.: 25-76127", "25-76127"),
            ("Trustee Sale No.: F25-1234-WA", "F25-1234-WA"),
        ):
            body = f"{raw}\nwill on 8/1/2026, at 9:00 AM at Courthouse sell at public auction"
            assert parse_nts_notice(body)["ts_number"] == want

    def test_ts_number_not_polluted_by_title_suffix(self):
        body = "TS #: 25-76127-NOTICE OF TRUSTEE'S SALE\nTS #: 25-76127"
        assert parse_nts_notice(body)["ts_number"] == "25-76127"

    def test_dotted_ampm_auction_time(self):
        for t in ("10:00 A.M.", "10:00 a.m.", "9:30 AM"):
            body = f"TS #: 25-1\nwill on 7/1/2026, at {t} at Pierce County Courthouse sell at public auction"
            p = parse_nts_notice(body)
            assert p["auction_date"] == "7/1/2026", t
            assert is_valid_nts(p), t

    def test_trustee_sale_number_line_not_read_as_trustee(self):
        body = (
            "Trustee Sale No.: 25-99999\n"
            "Current trustee of the deed of trust: Quality Loan Service Corp of WA\n"
            "will on 7/1/2026, at 9:00 AM at Courthouse sell at public auction"
        )
        assert parse_nts_notice(body)["trustee"] == "Quality Loan Service Corp of WA"

    def test_address_unit_prefix_preserved(self):
        body = "Commonly known as: UNIT B 123 MAIN ST\nTACOMA, WASHINGTON 98402 which is subject to"
        addr = parse_nts_notice(body)["property_address"]
        assert addr.startswith("UNIT B 123 MAIN ST")
        assert "which is subject" not in addr

    def test_address_same_line_deed_text_excluded(self):
        body = "Commonly known as: 5 OAK AVE, TACOMA, WA 98402 which is subject to that certain Deed of Trust dated 1/1/2020"
        addr = parse_nts_notice(body)["property_address"]
        assert "Deed of Trust" not in addr and "which is subject" not in addr
        assert "5 OAK AVE" in addr


class TestCrawlExtraction:
    def test_extract_notice_urls_filters_and_dedupes(self):
        listing = '''
        <a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-76127-notice-of-trustees-sale/">x</a>
        <a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-76127-notice-of-trustees-sale/">dup</a>
        <a href="https://www.tacomadailyindex.com/2026/06/04/jane-doe-name-change/">not nts</a>
        <a href="https://www.tacomadailyindex.com/2026/06/03/ts-25-99999-notice-of-trustees-sale/">y</a>
        '''
        urls = extract_notice_urls(listing)
        assert len(urls) == 2  # deduped + non-NTS filtered out
        assert urls[0].endswith("ts-25-76127-notice-of-trustees-sale/")
        assert all("notice-of-trustee" in u for u in urls)

    def test_extract_notice_urls_rejects_offsite_host(self):
        # a syndicated/compromised link with an NTS-shaped path on another host
        # must NOT be crawled (Codex P2 host-pin)
        listing = (
            '<a href="https://evil.example.com/2026/06/05/ts-25-1-notice-of-trustees-sale/">x</a>'
            '<a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-2-notice-of-trustees-sale/">ok</a>'
        )
        urls = extract_notice_urls(listing)
        assert len(urls) == 1
        assert "tacomadailyindex.com" in urls[0]

    def test_extract_article_text_strips_chrome(self):
        html = "<html><nav>Menu</nav><article><p>TS #: 25-1</p><script>junk()</script><p>Body</p></article><footer>f</footer></html>"
        text = extract_article_text(html)
        assert "TS #: 25-1" in text and "Body" in text
        assert "junk()" not in text and "Menu" not in text


class TestNoticeToRow:
    def _parsed(self):
        return parse_nts_notice((Path(__file__).parent / "fixtures" / "nts_tacoma_25-76127.txt").read_text(encoding="utf-8"))

    def test_row_fields_and_normalized_address(self):
        # today BEFORE the 7/10/2026 auction -> active
        row = notice_to_row(self._parsed(), "http://x/ts-25-76127/", today=date(2026, 6, 12))
        assert row["source"] == "tacoma_daily_index"
        assert row["ts_number"] == "25-76127"
        assert row["county"] == "pierce" and row["state"] == "WA"
        assert row["auction_date"] == date(2026, 7, 10)
        assert row["is_active"] is True
        assert row["property_address_normalized"] is not None
        assert "98391" in row["property_address_normalized"]
        assert len(row["raw_hash"]) == 64
        assert row["source_url"] == "http://x/ts-25-76127/"

    def test_past_auction_is_inactive(self):
        # today AFTER the auction -> not matched (kept for audit)
        row = notice_to_row(self._parsed(), "http://x/", today=date(2026, 8, 1))
        assert row["is_active"] is False

    def test_invalid_notice_returns_none(self):
        assert notice_to_row(parse_nts_notice("site chrome only"), "http://x/", today=date(2026, 6, 12)) is None

    def test_raw_hash_stable_for_same_content(self):
        p = self._parsed()
        a = notice_to_row(p, "http://x/", today=date(2026, 6, 12))["raw_hash"]
        b = notice_to_row(p, "http://different-url/", today=date(2026, 6, 12))["raw_hash"]
        assert a == b  # hash is over content, not URL


class TestQualityLoanFormat:
    """Second REAL fixture: Quality Loan layout (whole header on one line, 'More
    commonly known as', 'Subject to' stop) — the CURRENT Tacoma Daily Index format."""

    def setup_method(self):
        self.p = parse_nts_notice(
            (Path(__file__).parent / "fixtures" / "nts_tacoma_quality_loan.txt").read_text(encoding="utf-8")
        )

    def test_ts_number_prefixed(self):
        assert self.p["ts_number"] == "WA-25-1032618-RM"

    def test_auction_date(self):
        assert self.p["auction_date"] == "7/17/2026"

    def test_trustee_not_ts_or_prose(self):
        # must be the company, NOT the TS# (one-line-layout trap) nor "undersigned Trustee,"
        assert self.p["trustee"] == "QUALITY LOAN SERVICE CORPORATION"

    def test_beneficiary_and_grantor(self):
        assert self.p["beneficiary"] == "Lakeview Loan Servicing, LLC"
        assert "TORYIAN M CARTER" in self.p["grantor"]

    def test_parcel(self):
        assert self.p["parcel"] == "5005002880"

    def test_address_clean_no_deed_overrun(self):
        addr = self.p["property_address"]
        assert "9016-9018" in addr and "LAKEWOOD" in addr and "98498" in addr
        assert "Subject to" not in addr and "Deed of Trust" not in addr
        # 'WASHINGTON BLVD' is a STREET name — must NOT be abbreviated to 'WA BLVD'
        # (Codex: a global WASHINGTON->WA rewrite corrupted the match key).
        assert "WASHINGTON BLVD" in addr.upper()

    def test_match_key_uses_full_street_name(self):
        from datetime import date as _d

        from src.scrapers.sources.nts_tacoma_index import notice_to_row
        row = notice_to_row(self.p, "x", _d(2026, 6, 12))
        # the key the matcher joins on must carry the real street, not 'WA BLVD'
        assert "WASHINGTON BLVD" in row["property_address_normalized"]

    def test_is_valid(self):
        assert is_valid_nts(self.p)
