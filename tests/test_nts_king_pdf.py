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
