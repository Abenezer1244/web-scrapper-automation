"""Phase 3 union ('combine') segment — request validation + SQL assembly.

Pure unit tests (no DB). The dedup-bucket / window-ranking behaviour
(strong-by-property_key, weak-by-dedup_hash, neither-as-singleton,
duplicate-preferred-last, per-bucket identity_strength) is DB-level and is
verified in CI against a real Postgres — this dev environment has no test DB.
Here we lock the invariants that live in Python and in the SQL text.
"""
import pytest
from pydantic import ValidationError

from src.api.routes.segments import _UNION_SQL
from src.api.schemas import SegmentUnionRequest


class TestSegmentUnionRequest:
    def test_single_type_ok(self):
        # Union of one list is valid — it dedupes that list across counties/jobs.
        req = SegmentUnionRequest(record_types=["probate"])
        assert req.record_types == ["probate"]
        assert req.counties is None

    def test_multiple_types_deduped_sorted(self):
        req = SegmentUnionRequest(
            record_types=["pre_foreclosure", "PROBATE", "probate"]
        )
        assert req.record_types == ["pre_foreclosure", "probate"]

    def test_db_driven_type_accepted(self):
        req = SegmentUnionRequest(record_types=["death_certificate"])
        assert req.record_types == ["death_certificate"]

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            SegmentUnionRequest(record_types=[])

    def test_malformed_slug_rejected(self):
        with pytest.raises(ValidationError):
            SegmentUnionRequest(record_types=["probate", "DROP;TABLE"])

    def test_counties_cleaned(self):
        req = SegmentUnionRequest(
            record_types=["probate"], counties=[" King ", "PIERCE", "king"]
        )
        assert req.counties == ["king", "pierce"]


class TestUnionSqlAssembly:
    def test_dedup_bucket_coalesces_strong_weak_singleton(self):
        # Strong dedupe by property_key, weak fall back to dedup_hash, neither
        # stands alone by id. NO pk:/dh: prefix on the hash branches (Codex:
        # lets an un-backfilled strong row coalesce by hash value).
        sql = _UNION_SQL.format(county_clause="")
        assert "COALESCE(r.property_key, r.dedup_hash, 'id:' || r.id::text)" in sql

    def test_identity_strength_per_bucket(self):
        sql = _UNION_SQL.format(county_clause="")
        assert "bool_or(property_key IS NOT NULL)" in sql
        assert "AS identity_strength" in sql

    def test_includes_all_rows_contactable_first_then_non_duplicate(self):
        # Contactable rank MUST come before is_duplicate so we never drop an
        # available phone/email (Codex); is_duplicate is only a tiebreak. Never
        # filter is_duplicate=false (would drop a lead whose only row is a dup).
        sql = _UNION_SQL.format(county_clause="")
        assert "c.is_duplicate ASC" in sql
        assert "is_duplicate = false" not in sql.lower()
        contactable_pos = sql.index("phone IS NOT NULL OR")
        dup_pos = sql.index("c.is_duplicate ASC")
        assert contactable_pos < dup_pos, "contactable rank must precede is_duplicate"

    def test_no_membership_or_intersection_having(self):
        # Union is inclusive over results, NOT the membership-overlap path.
        sql = _UNION_SQL.format(county_clause="")
        assert "property_list_membership" not in sql
        assert "HAVING" not in sql

    def test_tenant_scoped_on_every_table(self):
        sql = _UNION_SQL.format(county_clause="")
        assert "r.user_id = :uid" in sql
        assert "j.user_id = :uid" in sql
        assert "sc.user_id = :uid" in sql

    def test_county_clause_toggles(self):
        with_c = _UNION_SQL.format(county_clause="AND sc.county = ANY(:counties)")
        without_c = _UNION_SQL.format(county_clause="")
        assert "sc.county = ANY(:counties)" in with_c
        assert "sc.county = ANY(:counties)" not in without_c
        assert "{county_clause}" not in without_c

    def test_bounded_by_limit(self):
        assert "LIMIT :limit" in _UNION_SQL.format(county_clause="")
