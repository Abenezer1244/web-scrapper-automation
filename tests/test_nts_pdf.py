"""Tests for the Pacific Publishing PDF NTS ingestion adapter (src/scrapers/sources/nts_pdf.py).

Runs against a REAL saved Snohomish County Tribune legals PDF
(tests/fixtures/nts_snoho_tribune_2025-12-17.pdf) — no mocks. Validates the full
extract → normalize → split → (shared) parse pipeline plus the de-hyphenation and
split-boundary edge cases that real PDF text exposes.
"""
from datetime import date
from pathlib import Path

from src.scrapers.sources import nts_pdf
from src.scrapers.sources.nts_tacoma_index import (
    is_valid_nts,
    notice_to_row,
    parse_nts_notice,
)

_PDF = Path(__file__).parent / "fixtures" / "nts_snoho_tribune_2025-12-17.pdf"


def _blocks() -> list[str]:
    data = _PDF.read_bytes()
    return nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))


class TestExtract:
    def test_rejects_non_pdf(self):
        import pytest
        with pytest.raises(ValueError):
            nts_pdf.extract_pdf_text(b"<html>not a pdf</html>")

    def test_rejects_empty(self):
        import pytest
        with pytest.raises(ValueError):
            nts_pdf.extract_pdf_text(b"")

    def test_extracts_real_pdf_text(self):
        text = nts_pdf.extract_pdf_text(_PDF.read_bytes())
        assert "NOTICE OF TRUSTEE" in text.upper()
        assert len(text) > 5000  # the weekly legals section is substantial


class TestNormalize:
    def test_dehyphenates_lowercase_wrap(self):
        assert "Parcel" in nts_pdf.normalize_pdf_text("Par-\ncel Number")
        assert "under" in nts_pdf.normalize_pdf_text("un-\nder RCW")

    def test_dehyphenates_uppercase_wrap(self):
        # all-caps statutory words wrap too: SER-VICE, NORTHEAST
        assert "SERVICE" in nts_pdf.normalize_pdf_text("QUALITY LOAN SER-\nVICE CORP")
        assert "NORTHEAST" in nts_pdf.normalize_pdf_text("87TH STREET NORTH-\nEAST")

    def test_wrapped_identifier_stays_intact(self):
        # A TS#/parcel that wraps at a hyphen must survive WHOLE: neither merged
        # ('WA-251012820') nor truncated by a stray space ('WA-25- 1012820' → the TS#
        # regex would capture only 'WA-25-'). Codex caught the truncation case.
        out = nts_pdf.normalize_pdf_text("Trustee Sale No.: WA-25-\n1012820-SW Title Order")
        assert "WA-25-1012820-SW" in out
        assert "251012820" not in out          # not merged
        assert "WA-25- 1012820" not in out      # not truncated by a space
        p = parse_nts_notice("NOTICE OF TRUSTEE'S SALE " + out)
        assert p["ts_number"] == "WA-25-1012820-SW"  # full TS#, not 'WA-25-'

    def test_collapses_newlines_and_spaces(self):
        out = nts_pdf.normalize_pdf_text("a\n\n  b   c")
        assert out == "a b c"


class TestSplit:
    def test_does_not_oversplit_on_titlecase_boilerplate(self):
        # The notice BODY contains Title-Case "Notice of Trustee Sale" boilerplate; only
        # the ALL-CAPS possessive header may start a new block (Codex over-split guard).
        text = (
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-1 ... If this is an amended "
            "Notice of Trustee Sale providing a 45-day notice, mediation must be requested. "
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-2 ..."
        )
        blocks = nts_pdf.split_notice_blocks(text)
        assert len(blocks) == 2  # two real headers, the Title-Case mention is NOT a split

    def test_real_pdf_block_count(self):
        # the 2025-12-17 issue carries exactly 7 trustee-sale notices
        assert len(_blocks()) == 7

    def test_drops_non_notice_preamble(self):
        for b in _blocks():
            assert "NOTICE OF TRUSTEE'S SALE" in b


class TestParseRealBlocks:
    def setup_method(self):
        self.blocks = _blocks()
        self.parsed = [parse_nts_notice(b) for b in self.blocks]

    def test_majority_parse_valid(self):
        # 5 of the 7 are the dominant residential formats (Quality Loan / North Star);
        # the other 2 (a commercial-loan notice + an MTC reverse-mortgage layout) are
        # safely SKIPPED by is_valid_nts — never emitted with wrong data.
        valid = [p for p in self.parsed if is_valid_nts(p)]
        assert len(valid) >= 5

    def test_known_quality_loan_notice(self):
        by_ts = {p["ts_number"]: p for p in self.parsed}
        p = by_ts["WA-25-1012820-SW"]
        assert p["auction_date"] == "12/26/2025"
        assert p["parcel"] == "00509200200900"
        assert "LYNNWOOD" in p["property_address"] and "98087" in p["property_address"]
        assert p["trustee"] == "QUALITY LOAN SERVICE CORPORATION"

    def test_auction_on_the_steps_variant_parses(self):
        # this issue's Quality Loan notices use "at <time> On the Steps in Front of …"
        # (no "at" before the location) — the broadened _AUCTION must capture it.
        valid = [p for p in self.parsed if is_valid_nts(p)]
        assert all(p["auction_date"] == "12/26/2025" for p in valid)

    def test_notice_to_row_for_snoho(self):
        p = next(p for p in self.parsed if p["ts_number"] == "WA-25-1012820-SW")
        # the PDF crawler MUST pass its own source/county — defaulting to Tacoma/Pierce
        # would mislabel the notice and the county-scoped matcher would route it wrong.
        row = notice_to_row(
            p, source_url="http://x/snoho.pdf", today=date(2025, 12, 1),
            source="snohomish_tribune", county="snohomish",
        )
        assert row is not None
        assert row["ts_number"] == "WA-25-1012820-SW"
        assert row["source"] == "snohomish_tribune"
        assert row["county"] == "snohomish"
        assert row["state"] == "WA"
        assert row["auction_date"] == date(2025, 12, 26)
        assert row["is_active"] is True
        assert row["property_address_normalized"]
