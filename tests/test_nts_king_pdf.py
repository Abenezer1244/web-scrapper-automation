"""King County NTS ingestion from the Queen Anne & Magnolia News legals PDF.

Runs against a REAL saved King PDF (tests/fixtures/nts_queen_anne_news_2026-06-24.pdf)
— no mocks. These King papers use MONTH-NAME auction dates ("July 24, 2026"), which
the numeric-only auction parser silently dropped, leaving King with zero notices even
though the PDF is full of RCW 61.24 trustee sales. This locks the month-name support
(Step A) and its zero-regression contract for the numeric (Tacoma/Snohomish) path.
"""
import re
from datetime import date
from pathlib import Path

from src.scrapers.sources import nts_pdf
from src.scrapers.sources.nts_king_pdf import parse_king_notice
from src.scrapers.sources.nts_tacoma_index import _to_date, notice_to_row

_PDF = Path(__file__).parent / "fixtures" / "nts_queen_anne_news_2026-06-24.pdf"
_TODAY = date(2026, 6, 25)


def _blocks() -> list[str]:
    data = _PDF.read_bytes()
    return nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))


def _rows() -> list[dict]:
    # The King crawler task uses parse_king_notice (no-colon fields + surrogate key).
    rows = []
    for b in _blocks():
        row = notice_to_row(
            parse_king_notice(b), source_url=str(_PDF), today=_TODAY,
            source="queen_anne_news", county="king",
        )
        if row is not None:
            rows.append(row)
    return rows


class TestMonthNameDate:
    def test_parses_month_name(self):
        assert _to_date("July 24, 2026") == date(2026, 7, 24)
        assert _to_date("December 1, 2026") == date(2026, 12, 1)

    def test_still_parses_numeric(self):
        assert _to_date("7/24/2026") == date(2026, 7, 24)

    def test_rejects_garbage(self):
        assert _to_date("not a date") is None
        assert _to_date(None) is None


class TestKingRealPdf:
    def test_pdf_has_trustee_sales(self):
        text = nts_pdf.extract_pdf_text(_PDF.read_bytes()).lower()
        assert "61.24" in text and "trustee" in text

    def test_recovers_all_king_notices(self):
        # Was 0 (numeric-only auction) -> 3/5 (Step A month-name) -> 5/5 (Step B
        # no-colon fields + surrogate keys). Guard against silent regression.
        rows = _rows()
        assert len(rows) == 5, f"expected 5 King notices, got {len(rows)}"

    def test_king_rows_are_king_with_month_name_auction(self):
        for row in _rows():
            assert row["county"] == "king"
            # auction_date is a real date parsed from a month-name string.
            assert isinstance(row["auction_date"], date)
            assert row["ts_number"]  # (source, ts_number) is the upsert natural key

    def test_ts_number_is_real_or_surrogate(self):
        # Every King notice has a stable natural key: a real WA-format trustee
        # number OR a REF-/APN- surrogate (never null — a null breaks upsert dedup).
        for row in _rows():
            ts = row["ts_number"]
            assert re.match(r"^(?:WA[\w\-]+|REF-\d+|APN-[\d\-]+)$", ts), ts

    def test_affinia_no_colon_fields_extracted(self):
        # The Affinia (no-colon) block: grantor is the real homeowner (not the
        # colon-parser garbage), and the parcel is the exact assessor number.
        rows = {r["ts_number"]: r for r in _rows()}
        aff = rows["REF-20220225001105"]
        assert "Acosta" in (aff["grantor"] or "")
        assert aff["parcel"] == "555690-0240"


# ── 2026-07-01 issue: the live PDF that surfaced three parser defects (long-trust
# beneficiary blew the Affinia gate's {0,200} gap → colon-parser garbage → varchar(512)
# insert crash lost the notice; "The above property is" leaked into the address and
# poisoned the normalized match key; MTC's colon-less "More commonly known as" dropped
# the address entirely). Real PDF, real assertions — locks all three fixes.
_PDF_0701 = Path(__file__).parent / "fixtures" / "nts_queen_anne_news_2026-07-01.pdf"
_TODAY_0701 = date(2026, 7, 1)


def _rows_0701() -> dict[str, dict]:
    data = _PDF_0701.read_bytes()
    blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))
    rows = {}
    for b in blocks:
        row = notice_to_row(
            parse_king_notice(b), source_url=str(_PDF_0701), today=_TODAY_0701,
            source="queen_anne_news", county="king",
        )
        if row is not None:
            rows[row["ts_number"]] = row
    return rows


class TestKing20260701Pdf:
    def test_recovers_all_three_notices(self):
        # Was 2/3: the long-beneficiary Affinia block died at INSERT (>512 grantor).
        assert len(_rows_0701()) == 3

    def test_long_trust_beneficiary_affinia_block_parses_clean(self):
        # The block that was LOST live: beneficiary is a ~201-char securitization
        # trust name, which must not blow the Affinia gate.
        row = _rows_0701()["REF-20051214001872"]
        assert row["grantor"] == "Timothy C. Mcdonald and Tanya M. Mcdonald"
        assert (row["beneficiary"] or "").startswith("Wilmington Trust")
        assert row["trustee"] == "Affinia Default Services, LLC"
        assert row["parcel"] == "025700-0175-09"
        assert row["property_address"] == "2416 South 128th Street, Seatac, WA 98168"

    def test_no_boilerplate_suffix_in_addresses(self):
        # "…98168 The above property is" leaked into the address (and thus the
        # normalized match key, making a parcel-less notice unmatchable).
        for row in _rows_0701().values():
            addr = row["property_address"] or ""
            assert "above property" not in addr.lower(), addr
            assert "subject" not in addr.lower(), addr

    def test_mtc_colonless_commonly_known_as(self):
        # MTC layout: "More commonly known as 1814 FRANKLIN AVE E…" (no colon)
        # used to yield property_address=None.
        row = _rows_0701()["REF-20200710001874"]
        assert row["property_address"] == "1814 FRANKLIN AVE E, SEATTLE, WA 98102"

    def test_all_fields_fit_their_columns(self):
        limits = {"ts_number": 64, "parcel": 64, "property_address": 512,
                  "property_address_normalized": 512, "auction_location": 512,
                  "grantor": 512, "trustee": 255, "beneficiary": 255,
                  "auction_time": 16, "nod_date": 32, "source_url": 512}
        for row in _rows_0701().values():
            for field, limit in limits.items():
                value = row.get(field)
                if value is not None:
                    assert len(value) <= limit, (row["ts_number"], field, len(value))
