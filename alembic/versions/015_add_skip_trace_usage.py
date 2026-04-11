"""Sprint 4 Phase 2.5: per-user skip-trace usage counter.

Adds `users.skip_trace_used_this_month` + `users.skip_trace_period_start`
for tracking how many lookups each user has consumed in the current
billing cycle. The webhook ingest increments this counter and reports
usage to Stripe's Meter Events API only for lookups beyond the bundled
quota (defined in settings.SKIP_TRACE_BUNDLED_QUOTAS).

The counter is reset monthly by a Celery Beat task (matches the
existing reset_monthly_usage pattern).

Revision ID: 015
Revises: 014
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "skip_trace_used_this_month",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "skip_trace_period_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "skip_trace_period_start")
    op.drop_column("users", "skip_trace_used_this_month")
