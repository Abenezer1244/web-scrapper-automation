"""Phase 4 — _extract_tax_fields: source-gated, coerced structured tax fields.

Pure unit tests (no DB). Locks the extraction/coercion contract that BOTH the
live worker insert and the offline backfill share, so the filter columns can't
be poisoned by a malformed or non-King scrape.
"""
from datetime import UTC, datetime
from decimal import Decimal

from src.workers.tasks import _extract_tax_fields


def _king(**over):
    base = {
        "source": "king_county_delinquent_taxes",
        "delinquent_amount": 1234.5,
        "bill_year": "2021",
    }
    base.update(over)
    return base


class TestSourceGating:
    def test_king_tax_extracts(self):
        amt, yr = _extract_tax_fields(_king(), "tax_delinquent")
        assert amt == Decimal("1234.50")
        assert yr == 2021

    def test_non_tax_record_type_skipped(self):
        assert _extract_tax_fields(_king(), "probate") == (None, None)

    def test_wrong_source_skipped(self):
        # A recorder keyword-match tax row (Pierce/Kitsap) has no King source.
        ed = {"source": "eagleweb", "delinquent_amount": 999, "bill_year": "2020"}
        assert _extract_tax_fields(ed, "tax_delinquent") == (None, None)

    def test_missing_source_skipped(self):
        ed = {"delinquent_amount": 999, "bill_year": "2020"}
        assert _extract_tax_fields(ed, "tax_delinquent") == (None, None)

    def test_non_dict_enrichment_skipped(self):
        assert _extract_tax_fields(None, "tax_delinquent") == (None, None)
        assert _extract_tax_fields("oops", "tax_delinquent") == (None, None)


class TestSnohomishSource:
    """The widened source allowlist must trust Snohomish's exact emitted shape
    (delinquent_amount as a Decimal STRING, bill_year as an int) and nothing else."""

    def _snoho(self, **over):
        # Mirrors what SnohomishWATaxDelinquentScraper writes per parcel.
        base = {
            "source": "snohomish_county_delinquent_taxes",
            "delinquent_amount": "10464.62",  # str(Decimal) — no float drift
            "bill_year": 2024,
        }
        base.update(over)
        return base

    def test_snohomish_string_amount_and_int_year_extract(self):
        amt, yr = _extract_tax_fields(self._snoho(), "tax_delinquent")
        assert amt == Decimal("10464.62")
        assert yr == 2024

    def test_snohomish_only_for_tax_record_type(self):
        assert _extract_tax_fields(self._snoho(), "probate") == (None, None)

    def test_lookalike_source_still_ignored(self):
        # A scraper that copied the field names but isn't on the allowlist must
        # NOT have its tax fields trusted (the whole point of source-gating).
        ed = {
            "source": "some_other_county_taxes",
            "delinquent_amount": "10464.62",
            "bill_year": 2024,
        }
        assert _extract_tax_fields(ed, "tax_delinquent") == (None, None)


class TestCoercion:
    def test_string_amount_quantized(self):
        amt, _ = _extract_tax_fields(_king(delinquent_amount="500"), "tax_delinquent")
        assert amt == Decimal("500.00")

    def test_float_amount_no_binary_drift(self):
        # Decimal(str(0.1+0.2)) style — must not carry 0.30000000000000004.
        amt, _ = _extract_tax_fields(_king(delinquent_amount=0.1 + 0.2), "tax_delinquent")
        assert amt == Decimal("0.30")

    def test_negative_amount_rejected(self):
        amt, _ = _extract_tax_fields(_king(delinquent_amount=-5), "tax_delinquent")
        assert amt is None

    def test_absurd_amount_rejected(self):
        amt, _ = _extract_tax_fields(_king(delinquent_amount=10**12), "tax_delinquent")
        assert amt is None

    def test_garbage_amount_rejected(self):
        amt, _ = _extract_tax_fields(_king(delinquent_amount="N/A"), "tax_delinquent")
        assert amt is None

    def test_zero_amount_allowed(self):
        amt, _ = _extract_tax_fields(_king(delinquent_amount=0), "tax_delinquent")
        assert amt == Decimal("0.00")


class TestBillYear:
    def test_valid_year(self):
        _, yr = _extract_tax_fields(_king(bill_year="2019"), "tax_delinquent")
        assert yr == 2019

    def test_future_year_rejected(self):
        bad = str(datetime.now(UTC).year + 5)
        _, yr = _extract_tax_fields(_king(bill_year=bad), "tax_delinquent")
        assert yr is None

    def test_ancient_year_rejected(self):
        _, yr = _extract_tax_fields(_king(bill_year="1800"), "tax_delinquent")
        assert yr is None

    def test_empty_year_is_none(self):
        _, yr = _extract_tax_fields(_king(bill_year=""), "tax_delinquent")
        assert yr is None

    def test_garbage_year_is_none(self):
        _, yr = _extract_tax_fields(_king(bill_year="twenty"), "tax_delinquent")
        assert yr is None

    def test_amount_present_year_missing(self):
        ed = {"source": "king_county_delinquent_taxes", "delinquent_amount": 750}
        amt, yr = _extract_tax_fields(ed, "tax_delinquent")
        assert amt == Decimal("750.00")
        assert yr is None
