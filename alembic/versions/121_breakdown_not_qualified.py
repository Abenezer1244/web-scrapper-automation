"""jobs: breakdown_not_qualified, the run-count bucket for held code violations (121).

Lead qualification (src/api/lead_qualification.py) holds back a code violation whose
verified owner, parcel or mailing address could not be confirmed: never delivered or
billed. The run breakdown (106) partitions everything a run found, so those rows need
their own bucket or the snapshot would not add up and every such run would fall back
to the live reading.

Nullable, no default, no backfill: NULL on a snapshot frozen before this migration
means nothing was held, and run_breakdown.breakdown_from_job reads it as 0. Same lock
safety as 106: catalog-only ADD COLUMN under a short lock_timeout.

Forward-only in production: rolling back is a code revert (old code ignores the
column). downgrade() exists for test databases.

Revision ID: 121
Revises: 120
Create Date: 2026-10-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "121"
down_revision = "120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column("jobs", sa.Column("breakdown_not_qualified", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("jobs", "breakdown_not_qualified")
