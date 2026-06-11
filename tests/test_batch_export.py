"""Piece 2 Phase 2A.3 — batch completion barrier + combined export (pure).

DB-row behaviour (the barrier query, the combined dedup/overlap) runs in CI.
Here we lock the SQL shape, the helpers, and the sweep registration.
"""
from src.workers.batch_export import _COMBINED_SQL, _filing_sort_key, _label


class TestCombinedSql:
    def test_scoped_to_batch_jobs_not_history(self):
        # The combined CSV is over THIS batch's jobs, not all-history-by-type.
        assert "r.job_id = ANY(:job_ids)" in _COMBINED_SQL
        assert "r.user_id = :uid" in _COMBINED_SQL  # tenant-scoped

    def test_dedup_and_overlap_and_counties(self):
        assert "COALESCE(r.property_key, r.dedup_hash, 'id:' || r.id::text)" in _COMBINED_SQL
        assert "count(DISTINCT record_type) AS overlap_count" in _COMBINED_SQL
        assert "source_counties" in _COMBINED_SQL


class TestHelpers:
    def test_label(self):
        assert _label("pre_foreclosure") == "Pre-Foreclosure"
        assert _label("tax_delinquent") == "Tax Delinquent"
        assert _label("unknown_x") == "Unknown X"

    def test_filing_sort_recent_first(self):
        assert _filing_sort_key("06/01/2026") < _filing_sort_key("01/01/2026")
        assert _filing_sort_key("") == 0
        assert _filing_sort_key("nope") == 0


def test_sweep_registered_and_imports():
    # The barrier beat is wired and the task imports cleanly.
    from src.workers.scheduler import app, batch_completion_sweep  # noqa: F401

    assert "batch-completion-sweep" in app.conf.beat_schedule
    assert app.conf.beat_schedule["batch-completion-sweep"]["task"] == (
        "src.workers.scheduler.batch_completion_sweep"
    )
