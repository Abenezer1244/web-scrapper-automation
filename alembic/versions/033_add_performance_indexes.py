"""Add performance indexes (033).

Performance audit 2026-06-03. Pure index DDL — RLS-neutral (Codex review:
CREATE INDEX touches no RLS predicate and runs under the migration/owner role),
so it is safe to run before or after the in-flight RLS role cutover. 030/031 are
intentional no-op placeholders; 032 is the prior additive column. This stacks
cleanly after 032.

Built with CREATE INDEX CONCURRENTLY so index creation never takes a
write-blocking lock on the (potentially large) `results` table in production.
CONCURRENTLY cannot run inside a transaction, hence the autocommit_block().
IF NOT EXISTS / IF EXISTS keep the migration idempotent and re-runnable —
consistent with the repo's idempotent RLS runners.

Each index matches a real query path verified in the audit:
- ix_jobs_user_created                    jobs(user_id, created_at)
    → list_jobs: WHERE user_id ORDER BY created_at DESC
- ix_results_job_user_dup_created         results(job_id, user_id, is_duplicate, created_at)
    → get_results: stats aggregate + ORDER BY (is_duplicate, created_at)
- ix_scraper_configs_user_county_state_type  scraper_configs(user_id, lower(county), upper(state), record_type)
    → get_results sibling lookup uses lower(county)/upper(state) — expression index
- ix_pending_skip_trace_dispatch          pending_skip_trace_rows(status, trace_type, enqueued_at)
    → skip-trace dispatcher drain: WHERE status/trace_type ORDER BY enqueued_at
- ix_county_connectors_picker             county_connectors(active, health_status, state, county)
    → public /scrapers/connectors picker: WHERE active AND health_status IN (...) ORDER BY state, county

Revision ID: 033
Revises: 032
Create Date: 2026-06-03
"""

from alembic import op
from sqlalchemy import text

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


# Detects a leftover INVALID index (relname matches, pg_index.indisvalid =
# false) from a prior interrupted CONCURRENTLY build. Without this preflight,
# a re-run of CREATE INDEX CONCURRENTLY IF NOT EXISTS would no-op against the
# invalid index and let Alembic mark 033 applied while the perf index stays
# unusable (Codex review, Medium). We drop the invalid one first, then build.
_INVALID_INDEX_CHECK = text(
    "SELECT 1 FROM pg_class c "
    "JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)


# (index_name, create_ddl) — DROP is derived from the name in downgrade().
_INDEXES: list[tuple[str, str]] = [
    (
        "ix_jobs_user_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_jobs_user_created "
        "ON jobs (user_id, created_at)",
    ),
    (
        "ix_results_job_user_dup_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_job_user_dup_created "
        "ON results (job_id, user_id, is_duplicate, created_at)",
    ),
    (
        "ix_scraper_configs_user_county_state_type",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_scraper_configs_user_county_state_type "
        "ON scraper_configs (user_id, lower(county), upper(state), record_type)",
    ),
    (
        "ix_pending_skip_trace_dispatch",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pending_skip_trace_dispatch "
        "ON pending_skip_trace_rows (status, trace_type, enqueued_at)",
    ),
    (
        "ix_county_connectors_picker",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_county_connectors_picker "
        "ON county_connectors (active, health_status, state, county)",
    ),
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, ddl in _INDEXES:
            invalid = bind.execute(_INVALID_INDEX_CHECK, {"name": name}).scalar()
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(ddl)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ddl in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
