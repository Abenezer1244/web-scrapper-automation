"""Guard tests for the NTS DB matcher plumbing (scoring itself is in test_nts_matcher).

The candidate-load/write SQL is integration-level (needs a real DB); here we pin the
cheap invariants: empty input is a no-op (no DB touched) and the module imports +
registers its beat task cleanly.
"""
from src.workers.nts_matcher_task import match_results_inline


def test_inline_empty_is_noop_without_db():
    # `db` is never touched when there are no candidate rows
    assert match_results_inline(db=None, result_dicts=[]) == 0


def test_beat_task_registered():
    from src.workers import app
    assert "src.workers.nts_matcher_task.match_nts_notices" in app.tasks
