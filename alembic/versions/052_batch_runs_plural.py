"""Batch 2B foundation: plural runs per batch. (052)

On-demand 2A enforced UNIQUE(batch_id) on batch_runs ("one run per batch — Phase
2B must revisit"). Scheduled batches need N runs over time, so:

1. DROP uq_batch_runs_batch_id.
2. ADD a PARTIAL unique index on (batch_id) WHERE status IN ('pending','running')
   — keeps the at-most-one-ACTIVE-run race-safety the old constraint provided
   (dispatch fan-out + recovery rely on "the active run" being unambiguous)
   while allowing unlimited terminal history rows.
3. ADD scheduled_for (timestamptz, NULL = on-demand run) + UNIQUE(batch_id,
   scheduled_for) — durable OCCURRENCE idempotency (Codex P1: the scheduler's
   ±1-minute due window can re-fire a batch whose run completed in <1 minute;
   active-run dedupe dies at terminal, this unique does not). Postgres treats
   NULLs as distinct, so on-demand runs are unconstrained by it.

Additive + a constraint swap; no rewrite, no backfill. Existing rows all have
scheduled_for NULL (they were on-demand). Safe under a rolling deploy: old code
never creates two active runs for one batch (it pre-creates at most one), so the
narrower partial index cannot be violated by in-flight old writers.

Revision ID: 052
Revises: 051
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batch_runs",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("uq_batch_runs_batch_id", "batch_runs", type_="unique")
    op.create_index(
        "uq_batch_runs_one_active",
        "batch_runs",
        ["batch_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_unique_constraint(
        "uq_batch_runs_occurrence", "batch_runs", ["batch_id", "scheduled_for"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_batch_runs_occurrence", "batch_runs", type_="unique")
    op.drop_index("uq_batch_runs_one_active", table_name="batch_runs")
    # Restoring the old single-run constraint requires at most one run per batch;
    # delete history rows first if downgrading past 2B (manual).
    op.create_unique_constraint("uq_batch_runs_batch_id", "batch_runs", ["batch_id"])
    op.drop_column("batch_runs", "scheduled_for")
