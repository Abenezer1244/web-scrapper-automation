"""Piece 2 Phase 2A.3 — batch completion barrier + combined export (pure).

DB-row behaviour (the barrier query, the combined dedup/overlap) runs in CI.
Here we lock the SQL shape, the helpers, and the sweep registration.
"""
import uuid

from src.workers.batch_export import (
    _COMBINED_SQL,
    _FAILED_CHILDREN_SQL,
    _label,
)


class TestCombinedSql:
    def test_scoped_to_batch_jobs_not_history(self):
        # The combined CSV is over THIS batch's jobs, not all-history-by-type.
        # uuid columns vs str/list params MUST be cast or psycopg2 has no
        # `uuid = text` / `uuid = ANY(text[])` operator (prod barrier failure).
        assert "r.job_id = ANY(CAST(:job_ids AS uuid[]))" in _COMBINED_SQL
        assert "r.user_id = CAST(:uid AS uuid)" in _COMBINED_SQL  # tenant-scoped + cast

    def test_buckets_are_prefixed_and_type_scoped(self):
        # Overlap identity is property_key ONLY. dedup_hash buckets carry the
        # record_type so a weak name+date hash can never merge two record types
        # (fake overlap + silently dropped row — Codex P1).
        assert "'pk:' || r.property_key" in _COMBINED_SQL
        assert "'dh:' || sc.record_type || ':' || r.dedup_hash" in _COMBINED_SQL
        assert "'id:' || r.id::text" in _COMBINED_SQL
        assert "COALESCE(r.property_key, r.dedup_hash" not in _COMBINED_SQL

    def test_overlap_count_is_property_key_only(self):
        assert (
            "CASE WHEN bucket LIKE 'pk:%' THEN count(DISTINCT record_type) ELSE 1 END"
            in _COMBINED_SQL
        )

    def test_mode_filter_and_deterministic_order(self):
        # overlaps_only filters in SQL BEFORE LIMIT (Codex P1: a Python filter
        # after the 50k cap could miss real overlaps + lie in counts).
        assert ":overlaps_only" in _COMBINED_SQL
        assert "ORDER BY a.overlap_count DESC" in _COMBINED_SQL
        assert "LIMIT :limit OFFSET :offset" in _COMBINED_SQL

    def test_counts_sql_shape(self):
        from src.workers.batch_export import _DELIVERY_COUNTS_SQL

        assert "count(*) AS leads_total" in _DELIVERY_COUNTS_SQL
        assert "overlaps_delivered" in _DELIVERY_COUNTS_SQL
        assert "singletons_suppressed" in _DELIVERY_COUNTS_SQL
        assert "unmatchable_no_parcel" in _DELIVERY_COUNTS_SQL
        assert "LIMIT" not in _DELIVERY_COUNTS_SQL  # counts are UNCAPPED


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
                if "LIMIT :limit" in sql:
                    params["limit"] = 10
                    params["offset"] = 0
                if ":overlaps_only" in sql:
                    params["overlaps_only"] = True
                if f":{TAX_CAP_BIND}" in sql:
                    params[TAX_CAP_BIND] = tax_cap_min_year(datetime.now(UTC).date())
                db.execute(text(sql), params).fetchall()
            db.rollback()

    def test_combined_sql_executes(self):
        self._run(_COMBINED_SQL)

    def test_delivery_counts_sql_executes(self):
        from src.workers.batch_export import _DELIVERY_COUNTS_SQL

        self._run(_DELIVERY_COUNTS_SQL)

    def test_failed_children_sql_executes(self):
        self._run(_FAILED_CHILDREN_SQL)


class TestCombinedSqlColumnCompleteness:
    """The combined SELECT must carry every column the CSV builder consumes —
    an under-selected set silently blanks populated columns AND (missing
    delinquent_bill_year) ships a fabricated synthetic tax date."""

    def test_selects_full_lead_column_set(self):
        for col in (
            "r.delinquent_bill_year", "r.delinquent_amount", "r.heirs",
            "r.legal_description", "r.doc_type", "r.phones", "r.emails",
            "r.absentee_owner", "r.out_of_state_owner", "r.owner_state",
            "r.auction_date", "r.default_amount", "r.enrichment_data",
            "r.date_recorded_parsed",
        ):
            assert col in _COMBINED_SQL, f"combined SELECT is missing {col}"


class TestDeliverySummary:
    """#3: the overlaps_only summary must not lump no-parcel rows into 'single-list'."""

    def test_no_parcel_reported_separately_not_as_single_list(self):
        from src.workers.batch_export import _delivery_summary
        msg = _delivery_summary(
            "overlaps_only",
            {"leads_total": 30, "overlaps_delivered": 7,
             "singletons_suppressed": 21, "unmatchable_no_parcel": 2},
        )
        assert "7 lead(s) found on 2 or more lists." in msg
        assert "21 single-list lead(s)" in msg
        assert "2 lead(s) had no parcel number" in msg
        assert "23 single-list" not in msg  # would be the old (total-overlaps) bug

    def test_zero_overlap_message_unchanged(self):
        from src.workers.batch_export import _delivery_summary
        msg = _delivery_summary(
            "overlaps_only",
            {"leads_total": 5, "overlaps_delivered": 0,
             "singletons_suppressed": 3, "unmatchable_no_parcel": 2},
        )
        assert "0 cross-list overlap leads found across 5 scraped leads." in msg


class TestHelpers:
    def test_label(self):
        assert _label("pre_foreclosure") == "Pre-Foreclosure"
        assert _label("tax_delinquent") == "Tax Delinquent"
        assert _label("unknown_x") == "Unknown X"


def test_sweep_registered_and_imports():
    # The barrier beat is wired and the task imports cleanly.
    from src.workers.scheduler import app, batch_completion_sweep  # noqa: F401

    assert "batch-completion-sweep" in app.conf.beat_schedule
    assert app.conf.beat_schedule["batch-completion-sweep"]["task"] == (
        "src.workers.scheduler.batch_completion_sweep"
    )
