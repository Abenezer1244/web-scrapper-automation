"""jobs.scheduled_for + unique (scraper_config_id, scheduled_for) index (079)

Durable occurrence idempotency for SCHEDULED single-config dispatch (Codex P1).
The scheduler's ±1-minute due window can fire adjacent beat ticks, and its
active-run dedupe dies once a fast scrape reaches a terminal status — so two
ticks (or two concurrent beats) could both create a job for the same occurrence.
This mirrors the batch path's uq_batch_runs_occurrence: a unique
(scraper_config_id, scheduled_for) makes the second insert a no-op via
ON CONFLICT DO NOTHING.

scheduled_for is the occurrence timestamp (UTC, truncated to the run minute) that
the dispatcher sets on scheduled jobs. NULL = on-demand (manual / test / POST
/jobs / run-once), exempt because Postgres treats NULLs as distinct in a unique
index — so existing rows (all NULL after the add) and every future manual run
never conflict, and no backfill is needed.

Prod-safety: the column add is nullable + defaultless (fast, metadata-only), and
the unique index is built CREATE UNIQUE INDEX CONCURRENTLY so it never takes a
write-blocking ACCESS EXCLUSIVE lock on the hot `jobs` table. CONCURRENTLY cannot
run inside a transaction, hence the autocommit_block(). IF NOT EXISTS + the
invalid-index preflight keep the migration idempotent / re-runnable (mirrors 033).

Revision ID: 079
Revises: 078
Create Date: 2026-07-02
"""

from alembic import op
from sqlalchemy import text

revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_jobs_scheduled_occurrence"

# Detects a leftover INVALID index (relname matches, pg_index.indisvalid = false)
# from a prior interrupted CONCURRENTLY build. Without this, a re-run of CREATE
# INDEX CONCURRENTLY IF NOT EXISTS no-ops against the invalid index and lets
# Alembic mark 079 applied while the unique guard is unusable (same guard as 033).
_INVALID_INDEX_CHECK = text(
    "SELECT 1 FROM pg_class c "
    "JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)


def upgrade() -> None:
    # Fast metadata-only add (nullable, no default). IF NOT EXISTS so a retry
    # after a failed CONCURRENTLY step below doesn't trip on the column.
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scheduled_for "
        "timestamp with time zone"
    )
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        if bind.execute(_INVALID_INDEX_CHECK, {"name": _INDEX_NAME}).scalar():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        # All existing scheduled_for are NULL (just added) and NULLs are distinct,
        # so this can never fail on pre-existing duplicates.
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON jobs (scraper_config_id, scheduled_for)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS scheduled_for")
