"""Trustee Sale finalizer — fail-closed contract (pure, no DB).

The UPDATE / verification SQL is Postgres-specific (jsonb ops) and exercised in CI /
live e2e. Here we pin the cheap, DB-free invariants: the ``nts_source`` -> UPDATE-param
mapping, the required-notice_id fail-closed guard, and the nts blob shape (must match
what nts_matcher_task writes so exports read enrichment_data['nts'] identically).
"""
import json
from datetime import date

import pytest

from src.workers.trustee_sale_finalize import (
    TrusteeSaleFinalizeError,
    _nts_update_params,
    _sibling_duplicate_ids,
    finalize_trustee_sale_job,
)

_FULL_SRC = {
    "notice_id": "notice-xyz-1",
    "auction_date": "2026-08-15",
    "default_amount": "282345.67",
    "ts_number": "WA-24-777",
    "trustee": "Quality Loan Service",
    "beneficiary": "US Bank",
    "auction_time": "10:00 AM",
    "auction_location": "County Courthouse",
    "source": "tacoma_daily_index",
    "source_url": "https://example.com/n",
}


class TestFailClosedContract:
    def test_missing_notice_id_raises(self):
        with pytest.raises(TrusteeSaleFinalizeError):
            _nts_update_params("rid-1", {})

    def test_empty_notice_id_raises(self):
        with pytest.raises(TrusteeSaleFinalizeError):
            _nts_update_params("rid-1", {"notice_id": "", "auction_date": "2026-08-15"})


class TestParamMapping:
    def test_core_params(self):
        p = _nts_update_params("rid-9", _FULL_SRC)
        assert p["rid"] == "rid-9"
        assert p["notice_id"] == "notice-xyz-1"
        assert p["auction_date"] == "2026-08-15"
        assert p["default_amount"] == "282345.67"

    def test_null_default_amount_passes_through(self):
        p = _nts_update_params("rid-9", {**_FULL_SRC, "default_amount": None})
        assert p["default_amount"] is None

    def test_nts_blob_shape_matches_matcher(self):
        p = _nts_update_params("rid-9", _FULL_SRC)
        blob = json.loads(p["nts"])
        # Same keys nts_matcher_task._write_match writes.
        assert set(blob) == {
            "ts_number", "trustee", "beneficiary", "auction_time",
            "auction_location", "source", "source_url", "matched_at", "confidence",
        }
        assert blob["ts_number"] == "WA-24-777"
        assert blob["trustee"] == "Quality Loan Service"
        # Exact source row, not a fuzzy match.
        assert blob["confidence"] == 1.0


class TestSiblingCollapse:
    def _row(self, id_, parcel, dhash, d):
        return {"id": id_, "parcel_id": parcel, "dedup_hash": dhash, "auction_date": d}

    def test_same_parcel_diff_address_collapses_keeping_soonest(self):
        # Same parcel, DIFFERENT dedup_hash (situs text drift) -> still one property.
        rows = [
            self._row("a", "0519285029", "hashA", date(2026, 9, 1)),
            self._row("b", "051928-5029", "hashB", date(2026, 8, 1)),  # soonest -> kept
        ]
        # keeps 'b' (soonest auction), marks 'a' duplicate
        assert _sibling_duplicate_ids(rows) == ["a"]

    def test_different_parcels_do_not_collapse(self):
        rows = [
            self._row("a", "0519285029", "hashA", date(2026, 9, 1)),
            self._row("b", "9999999999", "hashB", date(2026, 8, 1)),
        ]
        assert _sibling_duplicate_ids(rows) == []

    def test_parcelless_rows_fall_back_to_dedup_hash(self):
        # No usable parcel -> group by dedup_hash. Same hash collapses; different not.
        same = [
            self._row("a", None, "hashX", date(2026, 9, 1)),
            self._row("b", "", "hashX", date(2026, 8, 1)),
        ]
        assert _sibling_duplicate_ids(same) == ["a"]  # 'b' soonest kept
        diff = [
            self._row("a", None, "hashX", date(2026, 9, 1)),
            self._row("b", None, "hashY", date(2026, 8, 1)),
        ]
        assert _sibling_duplicate_ids(diff) == []

    def test_three_same_parcel_keeps_one(self):
        rows = [
            self._row("a", "P100", "h1", date(2026, 9, 1)),
            self._row("b", "P100", "h2", date(2026, 7, 1)),  # soonest -> kept
            self._row("c", "P100", "h3", date(2026, 8, 1)),
        ]
        assert sorted(_sibling_duplicate_ids(rows)) == ["a", "c"]


class TestModule:
    def test_finalizer_importable(self):
        assert callable(finalize_trustee_sale_job)
