"""Parser tests for the Tacoma Daily Index NTS parser.

Runs against a REAL saved notice (tests/fixtures/nts_tacoma_25-76127.txt — Pierce
County NTS, fetched 2026-06-12), per the no-mocks rule. Pins the investor-critical
field extraction.
"""
from decimal import Decimal
from pathlib import Path

from src.scrapers.sources.nts_tacoma_index import is_valid_nts, parse_nts_notice

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
