"""Batch 4 H5: track records-usage billing period on each user.

Adds users.records_period_start (nullable timestamp). Mirrors the
existing users.skip_trace_period_start column. The new scheduler
task reset_monthly_usage runs DAILY instead of on a
day_of_month=1 crontab, and for each user whose
records_period_start points at a month earlier than the current
month, it clears records_used and advances records_period_start
to the first of the current month. This makes the reset robust
against Celery Beat downtime on the 1st — if Beat is offline at
midnight, the next daily run catches up.

Revision ID: 020
Revises: 019
Create Date: 2026-04-11
"""

from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "records_period_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Backfill existing users with the start of the current month so
    # the first daily reset run after deploy doesn't immediately
    # reset everyone (that would be the old behavior repeated).
    op.execute("""
        UPDATE users
        SET records_period_start = date_trunc('month', NOW())
        WHERE records_period_start IS NULL
    """)


def downgrade() -> None:
    op.drop_column("users", "records_period_start")
