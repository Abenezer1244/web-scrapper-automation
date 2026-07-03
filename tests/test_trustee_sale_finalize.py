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
    # Collapse by dedup_hash (the app-wide billing key) — trustee_sale dedups exactly
    # like every other list, no more aggressively (product decision 2026-07-03).
    def _row(self, id_, dhash, d):
        return {"id": id_, "dedup_hash": dhash, "auction_date": d}

    def test_same_hash_collapses_keeping_soonest(self):
        rows = [
            self._row("a", "hashX", date(2026, 9, 1)),
            self._row("b", "hashX", date(2026, 8, 1)),  # soonest -> kept
        ]
        assert _sibling_duplicate_ids(rows) == ["a"]

    def test_different_hashes_do_not_collapse(self):
        # Different dedup_hash (incl. same parcel + drifted address) => distinct leads,
        # matching how every other list bills them.
        rows = [
            self._row("a", "hashA", date(2026, 9, 1)),
            self._row("b", "hashB", date(2026, 8, 1)),
        ]
        assert _sibling_duplicate_ids(rows) == []

    def test_three_same_hash_keeps_one(self):
        rows = [
            self._row("a", "hashX", date(2026, 9, 1)),
            self._row("b", "hashX", date(2026, 7, 1)),  # soonest -> kept
            self._row("c", "hashX", date(2026, 8, 1)),
        ]
        assert sorted(_sibling_duplicate_ids(rows)) == ["a", "c"]

    def test_single_row_is_noop(self):
        assert _sibling_duplicate_ids([self._row("a", "hashX", date(2026, 9, 1))]) == []


class TestModule:
    def test_finalizer_importable(self):
        assert callable(finalize_trustee_sale_job)
