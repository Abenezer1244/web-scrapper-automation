"""Add trial_ends_at column to users table.

Revision ID: 005_add_trial
"""

from alembic import op
import sqlalchemy as sa

revision = "005_add_trial"
down_revision = None  # Will be set by Alembic chain
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "trial_ends_at")
