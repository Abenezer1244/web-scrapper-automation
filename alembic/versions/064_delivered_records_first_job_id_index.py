"""Index delivered_records.first_job_id for the idempotent dedup Step 2b. (064)

PR #59's cross-job dedup added a Step 2b query that, on a watchdog re-run, reclaims
the delivered_records rows THIS job already owns:

    SELECT dedup_hash FROM delivered_records WHERE first_job_id = :jid

`first_job_id` is a plain UUID column (NOT a foreign key) and was unindexed, so the
query seq-scans the whole delivered_records table (~81k rows and growing) on every
scrape. This adds a non-unique btree index on it. Unlike uq_results_job_fingerprint
(an ON CONFLICT arbiter that MUST exist) this is a filter-only index — its absence
merely slows the query, never breaks correctness.

A plain CREATE INDEX would take an ACCESS EXCLUSIVE lock on the live table for the
whole build inside the alembic txn (the 2026-06-13 backfill-vs-ALTER lock incident).
So, same rule as migration 062 / the owner-flag indexes:

  - LARGE table (prod): build it OUT-OF-BAND, CONCURRENTLY, via
    scripts/create_delivered_records_first_job_id_index.sql BEFORE this migration.
    This migration then finds it present+valid and no-ops.
  - SMALL table (test / CI / fresh install): no lock concern, so this migration
    builds it inline (instant on a tiny table).

Revision ID: 064
Revises: 063
Create Date: 2026-06-18
"""
from alembic import op
from sqlalchemy import text

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

_INDEX = "ix_delivered_records_first_job_id"
# Below this row estimate an inline (ACCESS EXCLUSIVE) build is effectively instant
# and safe; above it we require the CONCURRENTLY out-of-band build.
_SMALL_TABLE_ROWS = 50_000


def upgrade() -> None:
    conn = op.get_bind()

    # Check VALIDITY, not mere existence: a failed CONCURRENTLY build leaves an
    # index that exists but is invalid/not-ready.
    index_valid = conn.execute(
        text(
            "SELECT i.indisvalid AND i.indisready FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = :n"
        ),
        {"n": _INDEX},
    ).scalar()
    if index_valid is True:
        return  # built out-of-band and valid — nothing to do
    if index_valid is False:
        raise RuntimeError(
            f"{_INDEX} exists but is INVALID/not-ready (a failed CONCURRENTLY "
            f"build). DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}; then rebuild it "
            "out-of-band (large table) or re-run this migration (small table)."
        )
    # index_valid is None → the index does not exist → build it below.

    # reltuples is -1 on a never-analyzed table and 0 on a fresh/empty one; both
    # are "small". A populated prod table reports its ~row estimate.
    est = conn.execute(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'delivered_records'")
    ).scalar()
    est = 0 if est is None or est < 0 else est

    if est < _SMALL_TABLE_ROWS:
        op.create_index(_INDEX, "delivered_records", ["first_job_id"])
    else:
        raise RuntimeError(
            f"delivered_records has ~{est} rows: build {_INDEX} CONCURRENTLY "
            "out-of-band (scripts/create_delivered_records_first_job_id_index.sql) "
            "BEFORE running this migration — an inline CREATE would "
            "ACCESS-EXCLUSIVE-lock the live table."
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="delivered_records", if_exists=True)
