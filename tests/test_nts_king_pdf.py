"""King County NTS ingestion from the Queen Anne & Magnolia News legals PDF.

Runs against a REAL saved King PDF (tests/fixtures/nts_queen_anne_news_2026-06-24.pdf)
— no mocks. These King papers use MONTH-NAME auction dates ("July 24, 2026"), which
the numeric-only auction parser silently dropped, leaving King with zero notices even
though the PDF is full of RCW 61.24 trustee sales. This locks the month-name support
(Step A) and its zero-regression contract for the numeric (Tacoma/Snohomish) path.
"""
from datetime import date
from pathlib import Path

from src.scrapers.sources import nts_pdf
from src.scrapers.sources.nts_tacoma_index import (
    _to_date,
    notice_to_row,
    parse_nts_notice,
)

_PDF = Path(__file__).parent / "fixtures" / "nts_queen_anne_news_2026-06-24.pdf"
_TODAY = date(2026, 6, 25)


def _blocks() -> list[str]:
    data = _PDF.read_bytes()
    return nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))


def _rows() -> list[dict]:
    rows = []
    for b in _blocks():
        row = notice_to_row(
            parse_nts_notice(b), source_url=str(_PDF), today=_TODAY,
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

    def test_recovers_king_notices(self):
        # Was 0 before month-name support; the PDF's institutional-trustee (MTC /
        # Affinia-with-TS#) notices now ingest. Guard against silent regression to 0.
        rows = _rows()
        assert len(rows) >= 3, f"expected >=3 King notices, got {len(rows)}"

    def test_king_rows_are_king_with_month_name_auction(self):
        for row in _rows():
            assert row["county"] == "king"
            # auction_date is a real date parsed from a month-name string.
            assert isinstance(row["auction_date"], date)
            assert row["ts_number"]  # (source, ts_number) is the upsert natural key
