"""Index four FK columns that were unindexed, causing cascade-delete seq-scans. (069)

The 2026-06-22 test-data purge (delete all users except admin@bridgeleads.io)
timed out because several FK columns pointing at `results`/`jobs` had no index, so
each row deletion seq-scanned a sibling table once per parent row. The worst was:

    delivered_records.first_result_id -> results(id) ON DELETE SET NULL

`first_result_id` was unindexed, so deleting a result fired
`UPDATE delivered_records SET first_result_id = NULL WHERE first_result_id = $1`,
a full seq-scan of delivered_records (~82k rows) PER deleted result. This is a
latent production perf bug well beyond the purge: every result deletion in normal
operation pays it. The other three are the cascade FK columns on
`pending_skip_trace_rows` and `job_logs`, unindexed for the same reason.

All four are FILTER/cascade-only indexes (not ON CONFLICT arbiters): their absence
only slows DELETE/SET-NULL cascades, it never breaks correctness.

These were already built CONCURRENTLY out-of-band on prod during the purge, so on
prod this migration finds them present+valid and no-ops. Same large-table rule as
migration 064: a plain inline CREATE would take an ACCESS EXCLUSIVE lock on a live
table (the 2026-06-13 backfill-vs-ALTER lock incident), so on a LARGE table build
them out-of-band via scripts/create_purge_followup_fk_indexes.sql FIRST; on a SMALL
table (CI / fresh install) this migration builds them inline (instant).

Revision ID: 069
Revises: 068
Create Date: 2026-06-22
"""
from alembic import op
from sqlalchemy import text

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None

# (index_name, table, column)
_INDEXES = [
    ("ix_delivered_records_first_result_id", "delivered_records", "first_result_id"),
    ("ix_pending_skip_trace_rows_result_id", "pending_skip_trace_rows", "result_id"),
    ("ix_pending_skip_trace_rows_job_id", "pending_skip_trace_rows", "job_id"),
    ("ix_job_logs_job_id", "job_logs", "job_id"),
]
# An inline (ACCESS EXCLUSIVE) build is effectively instant + safe below these
# thresholds; above either, require the CONCURRENTLY out-of-band build. We gate on
# BOTH the row estimate AND the physical size, because reltuples is -1 on a
# never-analyzed table (a freshly restored LARGE table reports -1, not its real
# size) — so an unknown estimate falls back to the byte size, never "small".
_SMALL_TABLE_ROWS = 50_000
_SMALL_TABLE_BYTES = 32 * 1024 * 1024  # 32 MB

# Confirm the index is valid+ready AND actually on public.<table>(<column>) — a
# name-only check would wrongly no-op on a same-named index in another schema or
# one with a different shape. Returns True (good), False (exists but invalid), or
# NULL/None (no matching index — build it).
_VALIDITY_SQL = text(
    """
    SELECT i.indisvalid AND i.indisready
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = ic.relnamespace
    WHERE ic.relname = :idx
      AND n.nspname = 'public'
      AND i.indrelid = ('public.' || :tbl)::regclass
      AND i.indnkeyatts = 1
      AND EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = i.indrelid
          AND a.attnum = i.indkey[0]
          AND a.attname = :col
      )
    """
)


def _ensure_index(conn, index: str, table: str, column: str) -> None:
    index_valid = conn.execute(
        _VALIDITY_SQL, {"idx": index, "tbl": table, "col": column}
    ).scalar()
    if index_valid is True:
        return  # built out-of-band, valid, and correctly shaped — nothing to do
    if index_valid is False:
        raise RuntimeError(
            f"{index} exists but is INVALID/not-ready (a failed CONCURRENTLY "
            f"build). DROP INDEX CONCURRENTLY IF EXISTS {index}; then rebuild it "
            "out-of-band (large table) or re-run this migration (small table)."
        )
    # None → no valid index of the right shape exists → build it below.

    est = conn.execute(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"), {"t": table}
    ).scalar()
    if est is not None and est >= 0:
        is_small = est < _SMALL_TABLE_ROWS
    else:
        # never analyzed (-1) or missing: trust the physical size, not the estimate
        size_bytes = conn.execute(
            text("SELECT pg_relation_size(('public.' || :t)::regclass)"), {"t": table}
        ).scalar()
        is_small = (size_bytes or 0) < _SMALL_TABLE_BYTES

    if is_small:
        op.create_index(index, table, [column])
    else:
        raise RuntimeError(
            f"{table} is non-trivial (rows≈{est}): build {index} CONCURRENTLY "
            "out-of-band (scripts/create_purge_followup_fk_indexes.sql) BEFORE "
            "running this migration — an inline CREATE would ACCESS-EXCLUSIVE-lock "
            "the live table."
        )


def upgrade() -> None:
    conn = op.get_bind()
    for index, table, column in _INDEXES:
        _ensure_index(conn, index, table, column)


def downgrade() -> None:
    # WARNING: this drops the indexes even if they were built out-of-band before
    # the migration, reintroducing the cascade/SET-NULL seq-scans these fix (the
    # delivered_records.first_result_id scan can stall result deletions on the live
    # ~82k-row table). Standard Alembic reversibility is kept intentionally; only
    # downgrade past 069 if you accept that performance regression.
    for index, table, _column in _INDEXES:
        op.drop_index(index, table_name=table, if_exists=True)
