"""scraper_configs.paused_reason for entitlement reconciliation (070)

Nullable marker distinguishing system-paused (downgrade reconciliation) configs
from user-paused ones, so re-upgrade revives only what entitlement enforcement
paused. Additive; existing rows default NULL (no backfill).
"""
import sqlalchemy as sa
from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_configs",
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraper_configs", "paused_reason")
