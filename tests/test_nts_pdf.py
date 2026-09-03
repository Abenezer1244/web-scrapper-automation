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
        # 6 of the 7 now parse: the 5 dominant residential formats (Quality Loan /
        # North Star) plus the MTC notice recovered by the pre-header TS fix. The
        # remaining commercial-loan notice is safely SKIPPED by is_valid_nts — never
        # emitted with wrong data.
        valid = [p for p in self.parsed if is_valid_nts(p)]
        assert len(valid) >= 6

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
        #
        # This used to assert EVERY valid notice was dated 12/26/2025, which only held
        # because the seventh (MTC, "TS No WA07000249-25-1" printed BEFORE its header)
        # was being dropped for want of a TS number. It is recovered now, and the source
        # really does say "NOTICE IS HEREBY GIVEN that on January 16, 2026" for it — so
        # the date is asserted per notice instead of as a blanket property of the issue.
        by_ts = {p["ts_number"]: p for p in self.parsed if is_valid_nts(p)}
        assert by_ts, "no valid notices parsed"
        for ts, p in by_ts.items():
            assert p["auction_date"], ts  # every valid notice carries a date
        for ts in ("WA-25-1012820-SW", "WA-25-1018388-RM", "WA-25-1018467-SW"):
            assert by_ts[ts]["auction_date"] == "12/26/2025", ts

    def test_pre_header_ts_notice_is_recovered_from_this_issue_too(self):
        """The pre-header TS bug was not specific to the Test 4 issue — this fixture,
        already in the repo, was silently losing a notice to it as well."""
        by_ts = {p["ts_number"]: p for p in self.parsed}
        petersons = by_ts["WA07000249-25-1"]
        assert is_valid_nts(petersons)
        assert "LIJA PETERSONS" in petersons["grantor"]
        assert petersons["auction_date"] == "January 16, 2026"

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


# ── Pre-header TS number binding ──────────────────────────────────────────────
# A second REAL Snohomish Tribune PDF, saved because it mixes the two trustee
# layouts in one issue: Quality Loan prints "Trustee Sale No.: <x>" AFTER the
# statutory header, while North Star ("TS #: <x> Title Order #: <y>") and MTC /
# Trustee Corps ("TS No <x> TO No <y>") print it BEFORE. A header-only split gave
# every pre-header notice the FOLLOWING notice's TS number and dropped the last one
# outright. This is the source behind the "Test 4" list (job 90e5eb41), where 2 of 6
# delivered leads carried the wrong TS number.
_PDF_MIXED_LAYOUT = Path(__file__).parent / "fixtures" / "nts_snoho_tribune_2026-08-05.pdf"

# grantor fragment -> the TS number THIS notice prints, read off the source PDF by hand.
_EXPECTED_TS = {
    "Jhan R. Smith": "WA-22-945105-SW",        # Quality Loan  (TS after header)
    "Diane Boggio": "WA-25-1018467-SW",        # Quality Loan
    "DONALD GREEN": "WA-26-1036613-BB",        # Quality Loan
    "MACARIO G. TORRES": "WA-26-1048154-BB",   # Quality Loan
    "SHAWN M WEINTRAUB": "25-75913",           # North Star    (TS BEFORE header)
    "CASEY CATE": "26-78299",                  # North Star
    "KERRY VERNON PHELPS": "WA08000007-26-1",  # MTC           (TS BEFORE header)
    "NAYER KHADEMI": "WA09000110-25-1",        # MTC — was dropped entirely before the fix
}


def _mixed_layout_notices() -> dict[str, dict]:
    data = _PDF_MIXED_LAYOUT.read_bytes()
    blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))
    out: dict[str, dict] = {}
    for block in blocks:
        parsed = parse_nts_notice(block)
        grantor = parsed.get("grantor") or ""
        for key in _EXPECTED_TS:
            if key.lower() in grantor.lower():
                out[key] = parsed
    return out


class TestPreHeaderTsNumberBinding:
    def test_every_notice_is_found(self):
        assert set(_mixed_layout_notices()) == set(_EXPECTED_TS)

    def test_each_notice_carries_its_own_ts_number(self):
        """The regression: a notice must never inherit the NEXT notice's TS number."""
        notices = _mixed_layout_notices()
        actual = {k: v.get("ts_number") for k, v in notices.items()}
        assert actual == _EXPECTED_TS

    def test_ts_numbers_are_all_distinct(self):
        notices = _mixed_layout_notices()
        numbers = [v.get("ts_number") for v in notices.values()]
        assert len(set(numbers)) == len(numbers)

    def test_last_notice_is_not_dropped(self):
        """MTC's KHADEMI notice is last in the issue; its only TS number sits before
        its header, so pre-fix it parsed to ts_number=None and is_valid_nts rejected
        it — a real upcoming sale silently lost."""
        khademi = _mixed_layout_notices()["NAYER KHADEMI"]
        assert khademi.get("ts_number") == "WA09000110-25-1"
        assert is_valid_nts(khademi)

    def test_quality_loan_trailer_ts_is_not_stolen_by_the_next_notice(self):
        """Quality Loan repeats its OWN TS number in its trailer. The pre-header fix
        must not move that onto the following notice (the same bug in reverse)."""
        notices = _mixed_layout_notices()
        assert notices["Jhan R. Smith"]["ts_number"] == "WA-22-945105-SW"
        assert notices["Diane Boggio"]["ts_number"] == "WA-25-1018467-SW"

    def test_identity_stays_attached_to_the_right_property(self):
        """TS number, grantor and parcel must all come from the SAME notice."""
        notices = _mixed_layout_notices()
        assert notices["CASEY CATE"]["parcel"] == "008337-000-009-00"
        assert notices["SHAWN M WEINTRAUB"]["parcel"] == "010347-00-0086-00"
        assert notices["NAYER KHADEMI"]["parcel"] == "006855-001-004-00"


class TestTrailingIdentityHelpers:
    def test_leaves_a_block_that_ends_in_its_own_trailer_alone(self):
        block = (
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-22-945105-SW "
            "Sale Line: 916-939-0772 IDSPub #0314650 8/5/2026"
        )
        assert nts_pdf._detach_trailing_identity(block) == (block, "")

    def test_detaches_an_adjacent_pre_header_run(self):
        block = "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-1 body TS #: 26-78299 Title Order #: DEF-687559"
        body, carry = nts_pdf._detach_trailing_identity(block)
        assert carry == "TS #: 26-78299 Title Order #: DEF-687559"
        assert "26-78299" not in body

    def test_detaches_unconditionally_because_the_receiver_decides(self):
        """Detaching is not where the safety lives — split_notice_blocks decides whether
        to USE the run, and only gives it to a notice that states no TS number itself.
        That test is strictly stronger than inspecting the block being detached from."""
        block = "NOTICE OF TRUSTEE'S SALE Grantor: SOMEONE TS #: 26-78299"
        body, carry = nts_pdf._detach_trailing_identity(block)
        assert carry == "TS #: 26-78299"
        assert body == "NOTICE OF TRUSTEE'S SALE Grantor: SOMEONE"


class TestCarriedRunNeverOverridesAStatedNumber:
    """A pre-header run SUPPLIES an identity to a notice that prints one before its
    header; it must never OVERRIDE one printed after it. Both cases below were the
    original bug in mirror image, found by Codex on the first implementation."""

    def test_a_notices_own_trailer_is_not_pushed_onto_the_next_notice(self):
        text = (
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: CUR-1 Grantor(s): CURR ONE "
            "will on 1/2/2027, at 10:00 AM Steps sell at public auction "
            "TS No CUR-1 Title Order No ABC "
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: NEXT-2 Grantor(s): NEXT TWO "
            "will on 1/3/2027, at 10:00 AM Steps sell at public auction"
        )
        got = [parse_nts_notice(b)["ts_number"] for b in nts_pdf.split_notice_blocks(text)]
        assert got == ["CUR-1", "NEXT-2"]

    def test_chrome_before_the_first_header_cannot_hijack_the_first_notice(self):
        text = (
            "Weekly index / non-notice chrome TS No INDEX-1 "
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: REAL-1 Grantor(s): REAL ONE "
            "will on 1/2/2027, at 10:00 AM Steps sell at public auction"
        )
        blocks = nts_pdf.split_notice_blocks(text)
        assert len(blocks) == 1
        assert parse_nts_notice(blocks[0])["ts_number"] == "REAL-1"

    def test_a_notice_with_no_stated_number_still_receives_the_carried_one(self):
        """The case the whole repair exists for — must keep working."""
        text = (
            "TS No FIRST-1 TO No AAA "
            "NOTICE OF TRUSTEE'S SALE Grantor: ONE will on 1/2/2027, at 10:00 AM Steps "
            "sell at public auction TS #: SECOND-2 Title Order #: BBB "
            "NOTICE OF TRUSTEE'S SALE Grantor: TWO will on 1/3/2027, at 10:00 AM Steps "
            "sell at public auction"
        )
        got = [parse_nts_notice(b)["ts_number"] for b in nts_pdf.split_notice_blocks(text)]
        assert got == ["FIRST-1", "SECOND-2"]

    def test_pathological_identity_run_does_not_hang(self):
        """A run of identity-looking tokens followed by one non-matching word used to
        backtrack catastrophically (200 tokens hung for >2 minutes) — a malformed or
        hostile legals PDF could have stalled the crawler worker. Peeling one item at
        a time is linear; 2,000 tokens must finish effectively instantly."""
        import time

        blob = (
            "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-1 body "
            + "TS No X " * 2000
            + "junk"
        )
        started = time.perf_counter()
        body, carry = nts_pdf._detach_trailing_identity(blob)
        assert time.perf_counter() - started < 5.0
        assert (body, carry) == (blob, "")  # trailing "junk" means there is no run

    def test_identity_run_scan_is_bounded(self):
        """The scan window is bounded, so cost does not grow with block size."""
        import time

        blob = "NOTICE OF TRUSTEE'S SALE Trustee Sale No.: WA-1 " + ("filler " * 50000)
        started = time.perf_counter()
        assert nts_pdf._identity_run_start(blob) == len(blob)
        assert time.perf_counter() - started < 5.0
