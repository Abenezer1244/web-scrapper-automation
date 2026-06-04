"""Phase 3 intersection segment — request validation + SQL assembly.

Pure unit tests (no DB): the live results->jobs->scraper_configs roundtrip and
the window-function ranking are exercised in CI against a real Postgres, since
this dev environment has no test DB (design constraint). These cover the logic
that lives in Python: request validation semantics ("on both lists" needs 2+
distinct supported types, strong-identity only) and the optional county clause.
"""
import pytest
from pydantic import ValidationError

from src.api.routes.segments import _INTERSECTION_SQL
from src.api.schemas import SegmentIntersectionRequest


class TestSegmentIntersectionRequest:
    def test_two_distinct_types_ok(self):
        req = SegmentIntersectionRequest(record_types=["probate", "pre_foreclosure"])
        assert req.record_types == ["pre_foreclosure", "probate"]  # sorted + deduped
        assert req.counties is None

    def test_single_type_rejected(self):
        # An intersection of one list is meaningless — a list cannot overlap itself.
        with pytest.raises(ValidationError):
            SegmentIntersectionRequest(record_types=["probate"])

    def test_duplicate_type_collapses_to_one_then_rejected(self):
        # ["probate","probate"] is ONE distinct type -> not an intersection.
        with pytest.raises(ValidationError):
            SegmentIntersectionRequest(record_types=["probate", "probate"])

    def test_db_driven_type_accepted(self):
        # Record types are DB-driven and open-ended; death_certificate (King)
        # is a real connector type and must NOT be rejected (Codex review).
        req = SegmentIntersectionRequest(record_types=["probate", "death_certificate"])
        assert req.record_types == ["death_certificate", "probate"]

    def test_malformed_slug_rejected(self):
        # Shape guard: spaces / punctuation / injection-looking values are out.
        with pytest.raises(ValidationError):
            SegmentIntersectionRequest(record_types=["probate", "pre foreclosure"])
        with pytest.raises(ValidationError):
            SegmentIntersectionRequest(record_types=["probate", "DROP;TABLE"])

    def test_types_normalized_lower_sorted_deduped(self):
        req = SegmentIntersectionRequest(
            record_types=["  Pre_Foreclosure ", "PROBATE", "probate"]
        )
        assert req.record_types == ["pre_foreclosure", "probate"]

    def test_three_types_ok(self):
        req = SegmentIntersectionRequest(
            record_types=["probate", "pre_foreclosure", "tax_delinquent"]
        )
        assert req.record_types == ["pre_foreclosure", "probate", "tax_delinquent"]

    def test_counties_cleaned_lower_sorted_deduped(self):
        req = SegmentIntersectionRequest(
            record_types=["probate", "pre_foreclosure"],
            counties=[" King ", "PIERCE", "king"],
        )
        assert req.counties == ["king", "pierce"]

    def test_empty_counties_becomes_none(self):
        req = SegmentIntersectionRequest(
            record_types=["probate", "pre_foreclosure"],
            counties=["", "   "],
        )
        assert req.counties is None


class TestIntersectionSqlAssembly:
    def test_county_clause_present_when_filtered(self):
        sql = _INTERSECTION_SQL.format(county_clause="AND sc.county = ANY(:counties)")
        assert "sc.county = ANY(:counties)" in sql
        assert "{county_clause}" not in sql

    def test_county_clause_absent_when_unfiltered(self):
        # sc.county still appears in the SELECT (it is an output column); only
        # the county FILTER predicate must be absent when no counties are given.
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "sc.county = ANY(:counties)" not in sql
        assert "{county_clause}" not in sql

    def test_sql_constrains_by_tenant_strong_identity_and_types(self):
        # Non-negotiable predicates: tenant, strong-identity only, selected types.
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "r.user_id = :uid" in sql
        assert "r.property_key IS NOT NULL" in sql
        assert "sc.record_type = ANY(:types)" in sql

    def test_sql_sources_overlap_from_membership_in_query(self):
        # Overlap is computed in SQL via the membership rollup (not materialized
        # in Python) — Codex P2 perf fix. The subquery requires all N types.
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "FROM property_list_membership" in sql
        assert sql.count("HAVING count(DISTINCT record_type) = :n") == 2

    def test_sql_picks_one_representative_row(self):
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "row_number() OVER" in sql
        assert "WHERE rk.rn = 1" in sql
        # contactable-first ranking
        assert "phone IS NOT NULL OR" in sql

    def test_sql_aggregates_matched_types_and_overlap(self):
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "array_agg(DISTINCT record_type" in sql
        assert "count(DISTINCT record_type)" in sql

    def test_sql_enforces_intersection_within_filtered_scope(self):
        # agg HAVING = :n guarantees the intersection survives the county filter
        # (Codex P2 correctness fix), so overlap_count can never be < N.
        sql = _INTERSECTION_SQL.format(county_clause="AND sc.county = ANY(:counties)")
        # one HAVING in the membership subquery, one in the agg CTE
        assert sql.count("HAVING count(DISTINCT record_type) = :n") == 2

    def test_sql_is_bounded(self):
        sql = _INTERSECTION_SQL.format(county_clause="")
        assert "LIMIT :limit" in sql
