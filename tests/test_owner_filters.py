"""Owner-location filter predicate tests (src/api/owner_filters.py).

Asserts the tri-state IS TRUE / IS FALSE semantics (Codex): a definite filter
must exclude NULL/unknown rows, and must compile to `.is_(...)`, not truthiness.
"""
from src.api.owner_filters import build_owner_conditions
from src.db.models import Result


class TestBuildOwnerConditions:
    def test_no_filters_empty(self):
        assert build_owner_conditions(None, None) == []

    def test_absentee_true_only(self):
        conds = build_owner_conditions(True, None)
        assert len(conds) == 1
        # compiles to "absentee_owner IS true" — excludes false AND null
        assert "IS true" in str(conds[0].compile(compile_kwargs={"literal_binds": True}))

    def test_absentee_false_excludes_null(self):
        conds = build_owner_conditions(False, None)
        assert "IS false" in str(conds[0].compile(compile_kwargs={"literal_binds": True}))

    def test_both_filters(self):
        conds = build_owner_conditions(True, True)
        assert len(conds) == 2
        cols = {str(c.left) for c in conds}
        assert f"{Result.__tablename__}.absentee_owner" in cols
        assert f"{Result.__tablename__}.out_of_state_owner" in cols

    def test_out_of_state_independent(self):
        assert len(build_owner_conditions(None, True)) == 1
