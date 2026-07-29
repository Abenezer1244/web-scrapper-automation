"""Add trial_ends_at column to users table.

Revision ID: 005_add_trial
"""

import sqlalchemy as sa
from alembic import op

revision = "005_add_trial"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "trial_ends_at")
