"""Phase B — filing-date window for segment overlap/combine.

Pure unit tests (no DB), matching tests/test_segments_union.py: this dev env has
no test Postgres, so DB-level row behaviour is verified in CI. Here we lock the
Python window-resolution logic, the request validation, and the SQL text that the
windowed queries assemble.
"""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.api.routes.segments import (
    _EXCLUDED_NO_DATE_SQL,
    _INTERSECTION_DATED_SQL,
    _UNION_SQL,
    _resolve_filing_window,
)
from src.api.schemas import SegmentIntersectionRequest, SegmentUnionRequest


class TestResolveFilingWindow:
    def test_no_window_means_all_time(self):
        assert _resolve_filing_window(None, None, None) == (None, None, False)

    def test_lookback_sets_from_and_requires_date(self):
        ff, ft, require = _resolve_filing_window(90, None, None)
        assert ft is None and require is True
        assert ff == date.today() - timedelta(days=90)  # noqa: DTZ011 (UTC date)

    def test_explicit_range_requires_date(self):
        ff, ft, require = _resolve_filing_window(None, date(2026, 1, 1), date(2026, 6, 1))
        assert (ff, ft, require) == (date(2026, 1, 1), date(2026, 6, 1), True)

    def test_explicit_wins_over_lookback(self):
        # An explicit from/to overrides lookback_days (documented precedence).
        ff, _ft, require = _resolve_filing_window(365, date(2026, 1, 1), None)
        assert ff == date(2026, 1, 1) and require is True


class TestWindowRequestValidation:
    def test_lookback_bounds(self):
        assert SegmentUnionRequest(record_types=["probate"], lookback_days=1).lookback_days == 1
        with pytest.raises(ValidationError):
            SegmentUnionRequest(record_types=["probate"], lookback_days=0)
        with pytest.raises(ValidationError):
            SegmentUnionRequest(record_types=["probate"], lookback_days=3661)

    def test_inverted_explicit_range_rejected_union(self):
        with pytest.raises(ValidationError):
            SegmentUnionRequest(
                record_types=["probate"],
                filing_from=date(2026, 6, 1),
                filing_to=date(2026, 1, 1),
            )

    def test_inverted_explicit_range_rejected_intersection(self):
        with pytest.raises(ValidationError):
            SegmentIntersectionRequest(
                record_types=["probate", "pre_foreclosure"],
                filing_from=date(2026, 6, 1),
                filing_to=date(2026, 1, 1),
            )

    def test_equal_endpoints_ok(self):
        req = SegmentUnionRequest(
            record_types=["probate"],
            filing_from=date(2026, 1, 1),
            filing_to=date(2026, 1, 1),
        )
        assert req.filing_from == req.filing_to


class TestWindowedSqlAssembly:
    def test_union_has_optional_date_predicates(self):
        sql = _UNION_SQL.format(county_clause="")
        assert "r.date_recorded_parsed >= CAST(:filing_from AS date)" in sql
        assert "r.date_recorded_parsed <= CAST(:filing_to AS date)" in sql
        assert "CAST(:require_date AS boolean) = FALSE OR r.date_recorded_parsed IS NOT NULL" in sql

    def test_dated_intersection_is_results_based_not_membership(self):
        sql = _INTERSECTION_DATED_SQL.format(county_clause="")
        # Computes from results, NOT the membership rollup (which lacks filing date).
        assert "property_list_membership" not in sql
        assert "r.date_recorded_parsed IS NOT NULL" in sql
        assert "r.property_key IS NOT NULL" in sql
        assert "HAVING count(DISTINCT record_type) = :n" in sql

    def test_excluded_count_sql_filters_null_filing_date(self):
        sql = _EXCLUDED_NO_DATE_SQL.format(pk_clause="", county_clause="")
        assert "r.date_recorded_parsed IS NULL" in sql
        # property_key clause is injected only for intersection
        pk_sql = _EXCLUDED_NO_DATE_SQL.format(
            pk_clause="AND r.property_key IS NOT NULL", county_clause=""
        )
        assert "AND r.property_key IS NOT NULL" in pk_sql
