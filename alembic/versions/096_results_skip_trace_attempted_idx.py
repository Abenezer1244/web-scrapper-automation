"""Retention: index results(skip_trace_attempted_at, id) (096).

The skip-trace PII retention sweep (Privacy Policy §7) scans globally for rows
whose vendor contact data is past its retention window. The existing
ix_results_user_created (068) cannot serve it: it leads with user_id, so a
predicate on the timestamp alone can't range-scan it, and it is PARTIAL on
is_duplicate = false, which would silently exclude duplicate rows from the purge
entirely. A compliance sweep that skips rows is worse than no index.

Trailing id gives the sweep a deterministic batch order (ORDER BY
skip_trace_attempted_at, id), so repeated batches make forward progress instead
of re-reading the same rows.

This index is partial too, but on the column the sweep actually filters: the
purge predicate is `skip_trace_attempted_at < :cutoff`, which is unsatisfiable
for NULL, so a NULL row can never be an eligible row. That makes the partial
provably complete for this query. 068's predicate was on a DIFFERENT column
(is_duplicate) than the one being filtered, which is what made it lossy here.
Rows never traced keep skip_trace_attempted_at NULL and hold no vendor PII.

CONCURRENTLY (no write lock on the large results table) requires autocommit
(no txn). Idempotent + invalid-index preflight, per the 033/068 pattern.

Revision ID: 096
Revises: 095
Create Date: 2026-09-17
"""
from alembic import op
from sqlalchemy import text

revision = "096"
down_revision = "095"
branch_labels = None
depends_on = None

_INDEX = "ix_results_skip_trace_attempted"
_CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
    "ON results (skip_trace_attempted_at, id) "
    "WHERE skip_trace_attempted_at IS NOT NULL"
)
_INVALID_CHECK = text(
    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        if conn.execute(_INVALID_CHECK, {"name": _INDEX}).first():
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
        conn.execute(text(_CREATE))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.get_bind().execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
