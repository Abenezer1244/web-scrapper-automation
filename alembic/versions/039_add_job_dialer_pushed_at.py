"""Add jobs.dialer_pushed_at (039) — Phase 5 dialer-push sweep marker.

Additive, nullable. The dialer-push sweep (workers/scheduler.dialer_push_sweep)
claims a job by stamping this column, so each done job whose config has a
dialer_webhook_url is pushed to the dialer at most once — AFTER its async
skip-trace has settled (you can't push leads whose phone/DNC the Tracerfy
webhook hasn't filled in yet). NULL = not yet evaluated.

Revision ID: 039
Revises: 038
Create Date: 2026-06-05
"""
import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dialer_pushed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "dialer_pushed_at")
