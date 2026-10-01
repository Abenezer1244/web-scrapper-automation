"""skip_trace_queues: what a batch SENT, and what billing DECIDED (110).

Billing charges a batch's `unmatched` rows only when Tracerfy demonstrably accepted every
row we sent. Today it judges that by comparing `rows_uploaded` with the pending rows
STAMPED with the queue id. But `_persist_submission` stamps only the rows still pinned to
its claim, and only alerts when fewer moved than were sent. So with 4 sent, 3 stamped and
3 uploaded, a row was dropped and billing still says "all accepted" (Phase 1b-2, W3).

  rows_sent         how many rows the batch's POST carried (`len(claimed)`), written by
                    the dispatcher with the queue row. Billing compares `rows_uploaded`
                    with THIS.
  unmatched_billed  the decision billing made for the queue's `unmatched` rows, written
                    once, in the ingest transaction that bills. The contact-lookup
                    reconciler reads it rather than recomputing the rule (AA2), so an
                    action's page always states what billing did.

Both nullable, no default, no backfill, no CHECK (the 099/106 convention). NULL
`rows_sent` = recorded before this migration, and the true count is not recoverable: a
fabricated one would be billed against. Billing reads it as "not proven", so that queue
bills `completed` rows only (the owner's O-C rule, toward the customer). NULL
`unmatched_billed` = billing has not run on the queue, or ran before O-C.

SCHEMA FIRST (Codex consult AE1). This migration ships ALONE: no deployed code names these
columns until production is verified at 110. A worker on new code against a stale schema
would otherwise fail ingest's full ORM read of `SkipTraceQueue` and, after its retries,
mark a PAID batch `errored`.

Lock safety: nullable ADD COLUMN with no default is catalog-only (no rewrite), but it
still takes a brief ACCESS EXCLUSIVE lock on `skip_trace_queues`. lock_timeout makes the
boot migration fail fast instead of queueing the dispatcher and ingest behind it.

Forward-only in production: rolling back is a code revert (old code ignores the columns).
downgrade() exists for test databases.

Revision ID: 110
Revises: 109
Create Date: 2026-10-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "110"
down_revision = "109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column("skip_trace_queues", sa.Column("rows_sent", sa.Integer(), nullable=True))
    op.add_column(
        "skip_trace_queues", sa.Column("unmatched_billed", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("skip_trace_queues", "unmatched_billed")
    op.drop_column("skip_trace_queues", "rows_sent")
