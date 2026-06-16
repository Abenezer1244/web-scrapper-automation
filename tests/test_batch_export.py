"""Piece 2 Phase 2A.3 — batch completion barrier + combined export (pure).

DB-row behaviour (the barrier query, the combined dedup/overlap) runs in CI.
Here we lock the SQL shape, the helpers, and the sweep registration.
"""
import uuid

from src.workers.batch_export import (
    _COMBINED_SQL,
    _FAILED_CHILDREN_SQL,
    _filing_sort_key,
    _label,
)


class TestCombinedSql:
    def test_scoped_to_batch_jobs_not_history(self):
        # The combined CSV is over THIS batch's jobs, not all-history-by-type.
        # uuid columns vs str/list params MUST be cast or psycopg2 has no
        # `uuid = text` / `uuid = ANY(text[])` operator (prod barrier failure).
        assert "r.job_id = ANY(CAST(:job_ids AS uuid[]))" in _COMBINED_SQL
        assert "r.user_id = CAST(:uid AS uuid)" in _COMBINED_SQL  # tenant-scoped + cast

    def test_dedup_and_overlap_and_counties(self):
        assert "COALESCE(r.property_key, r.dedup_hash, 'id:' || r.id::text)" in _COMBINED_SQL
        assert "count(DISTINCT record_type) AS overlap_count" in _COMBINED_SQL
        assert "source_counties" in _COMBINED_SQL


class TestRawSqlExecutesOnPostgres:
    """Execute the raw export SQL against REAL Postgres via the SYNC/psycopg2
    session the worker actually uses — the only thing that reproduces
    `operator does not exist: uuid = text`. The async (asyncpg) fixture handles
    uuid params differently and would NOT catch it; pure string tests can't
    either. Runs in CI (no local Postgres). The uuid=text error fires at plan
    time, so no fixture rows are needed; both empty + non-empty job_ids are
    exercised (empty -> ANY(CAST('{}' AS uuid[])) must not error)."""

    def _run(self, sql: str):
        from datetime import UTC, datetime

        from sqlalchemy import text

        from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year
        from src.db.session import system_sync_session

        with system_sync_session() as db:
            for job_ids in ([str(uuid.uuid4())], []):
                params = {"uid": str(uuid.uuid4()), "job_ids": job_ids}
                if ":limit" in sql or "LIMIT :limit" in sql:
                    params["limit"] = 10
                # Bind the 18-month tax cap only for SQL that carries the fragment
                # (_COMBINED_SQL does; _FAILED_CHILDREN_SQL does not). text() raises
                # InvalidRequestError on a missing bind, so the placeholder presence
                # check keeps the shared helper correct for BOTH constants.
                if f":{TAX_CAP_BIND}" in sql:
                    params[TAX_CAP_BIND] = tax_cap_min_year(datetime.now(UTC).date())
                db.execute(text(sql), params).fetchall()
            db.rollback()

    def test_combined_sql_executes(self):
        self._run(_COMBINED_SQL)

    def test_failed_children_sql_executes(self):
        self._run(_FAILED_CHILDREN_SQL)


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
