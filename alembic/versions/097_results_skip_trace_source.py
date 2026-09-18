"""results.skip_trace_source: where a lead's settled skip-trace answer came from (097).

A lead an earlier run already delivered can be looked up again by a later run
(#342), and most of those answers are REUSED: copied from this account's earlier
answer for the same property, or from its own address cache, with no new
Tracerfy lookup bought. The Already delivered tab could not say so, because the
only record of "a lookup was bought for this row" is pending_skip_trace_rows,
which the API role has no grant on. This column puts it on the row the API can
read (bridgeleads_app has table-level SELECT on results).

  'lookup'  Tracerfy answered for this row (tracerfy_ingest).
  'reused'  the answer was copied from this account's earlier answer
            (enrich reuse x2, enqueue cache hit, dispatcher known-answer sweep).
  NULL      never settled, or settled before this column existed: unknown, and
            never counted as reused.

Written only together with a hit/miss status, in the same statement.

Lock safety: a nullable ADD COLUMN with no default is catalog-only (no rewrite),
but it still needs a brief ACCESS EXCLUSIVE lock. lock_timeout makes the boot
migration fail fast rather than queue every request behind a long-running
transaction on results. The CHECK is added NOT VALID (every existing row is NULL,
so it holds trivially) and VALIDATEd afterwards in its own transaction, which takes
only SHARE UPDATE EXCLUSIVE and never blocks reads or writes.

Revision ID: 097
Revises: 096
Create Date: 2026-09-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "097"
down_revision = "096"
branch_labels = None
depends_on = None

_CHECK = "ck_results_skip_trace_source"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column("results", sa.Column("skip_trace_source", sa.String(16), nullable=True))
    op.execute(text(
        f"ALTER TABLE results ADD CONSTRAINT {_CHECK} "
        "CHECK (skip_trace_source IN ('lookup', 'reused')) NOT VALID"
    ))
    with op.get_context().autocommit_block():
        op.get_bind().execute(text(f"ALTER TABLE results VALIDATE CONSTRAINT {_CHECK}"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text(f"ALTER TABLE results DROP CONSTRAINT IF EXISTS {_CHECK}"))
    op.drop_column("results", "skip_trace_source")
