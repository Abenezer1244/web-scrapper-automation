"""Analytics: partial index results(user_id, created_at) (068).

Phase 3 dashboard analytics windows by (user_id, created_at) over non-duplicate
leads. The existing ix_results_job_user_dup_created leads with job_id, so it
can't serve a (user_id, created_at) range scan. Partial WHERE is_duplicate=false
matches the endpoint's predicate exactly. CONCURRENTLY (no write lock on the
large results table) requires autocommit (no txn). Idempotent + invalid-index
preflight, per the 033 pattern.

Revision ID: 068
Revises: 067
Create Date: 2026-06-19
"""
from alembic import op
from sqlalchemy import text

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None

_INDEX = "ix_results_user_created"
_CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
    "ON results (user_id, created_at) WHERE is_duplicate = false"
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
