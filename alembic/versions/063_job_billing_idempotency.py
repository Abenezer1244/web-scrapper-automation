"""Idempotent billing markers on jobs. (063)

run_scrape_job increments users.records_used by the job's billable_count once.
A watchdog re-run re-reaches the billing step, so without a guard it would bill
the same job twice. These columns make billing a compare-and-set: only the
attempt that flips billing_applied_at from NULL increments the user counter, and
billed_count records what was charged (the authoritative amount to reverse if a
future cleanup ever needs it — the no-dedup_hash billable rows aren't derivable
from delivered_records, so it must be stored, not recomputed).

billed_count NOT NULL DEFAULT 0 (constant default = instant ALTER, no table
rewrite); billing_applied_at NULL. Both on the RLS-FORCE'd jobs table — no policy
change.

Revision ID: 063
Revises: 062
Create Date: 2026-06-17
"""
import sqlalchemy as sa
from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("billed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("billing_applied_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "billing_applied_at")
    op.drop_column("jobs", "billed_count")
