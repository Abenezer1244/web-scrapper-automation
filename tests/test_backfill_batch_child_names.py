"""Guard on the name-backfill's pre-write validation.

The ops script rewrites user-visible scraper names in production, so its refusal
conditions are worth pinning. validate() is pure — it takes the plan and a
snapshot of every config and models the POST-APPLY state, so it needs no DB.
"""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backfill_batch_child_names",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_batch_child_names.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
validate = _MOD.validate

U = "11111111-1111-1111-1111-111111111111"
V = "22222222-2222-2222-2222-222222222222"


class TestValidate:
    def test_accepts_a_plan_that_resolves_every_collision(self):
        plan = [
            {"id": "a", "user_id": U, "old": "King Probate (batch)", "new": "Q3 - King Probate"},
            {"id": "b", "user_id": U, "old": "King Probate (batch)", "new": "Q4 - King Probate"},
        ]
        current = [
            {"id": "a", "user_id": U, "name": "King Probate (batch)"},
            {"id": "b", "user_id": U, "name": "King Probate (batch)"},
        ]
        validate(plan, current)   # no raise

    def test_rejects_a_plan_that_collides_with_itself(self):
        plan = [
            {"id": "a", "user_id": U, "old": "x", "new": "Same - King Probate"},
            {"id": "b", "user_id": U, "old": "x", "new": "Same - King Probate"},
        ]
        current = [
            {"id": "a", "user_id": U, "name": "x"},
            {"id": "b", "user_id": U, "name": "x"},
        ]
        with pytest.raises(SystemExit, match="still collide"):
            validate(plan, current)

    def test_rejects_a_rebuilt_name_that_hits_an_UNTOUCHED_config(self):
        # The case Codex caught: comparing planned names only to each other passes
        # here, but config "c" is left alone and already owns that exact name — so
        # applying would swap one collision for a brand-new one.
        plan = [
            {"id": "a", "user_id": U, "old": "King Probate (batch)", "new": "Q3 - King Probate"},
            {"id": "b", "user_id": U, "old": "King Probate (batch)", "new": "Q4 - King Probate"},
        ]
        current = [
            {"id": "a", "user_id": U, "name": "King Probate (batch)"},
            {"id": "b", "user_id": U, "name": "King Probate (batch)"},
            {"id": "c", "user_id": U, "name": "Q3 - King Probate"},   # never planned
        ]
        with pytest.raises(SystemExit, match="still collide"):
            validate(plan, current)

    def test_same_name_under_a_DIFFERENT_user_is_fine(self):
        # The dashboard list is per-user, so cross-tenant name reuse is not a clash.
        plan = [{"id": "a", "user_id": U, "old": "x", "new": "Q3 - King Probate"}]
        current = [
            {"id": "a", "user_id": U, "name": "x"},
            {"id": "z", "user_id": V, "name": "Q3 - King Probate"},
        ]
        validate(plan, current)   # no raise

    def test_rejects_the_same_config_twice(self):
        plan = [
            {"id": "a", "user_id": U, "old": "x", "new": "One"},
            {"id": "a", "user_id": U, "old": "x", "new": "Two"},
        ]
        with pytest.raises(SystemExit, match="twice"):
            validate(plan, [{"id": "a", "user_id": U, "name": "x"}])

    def test_empty_plan_is_a_no_op(self):
        validate([], [{"id": "a", "user_id": U, "name": "dup"},
                      {"id": "b", "user_id": U, "name": "dup"}])

    def test_comparison_ignores_case_and_surrounding_space(self):
        plan = [{"id": "a", "user_id": U, "old": "x", "new": "  Q3 - King Probate  "}]
        current = [
            {"id": "a", "user_id": U, "name": "x"},
            {"id": "c", "user_id": U, "name": "q3 - king probate"},
        ]
        with pytest.raises(SystemExit, match="still collide"):
            validate(plan, current)
