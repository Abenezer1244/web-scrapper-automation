"""Add users.notification_prefs (per-user email notification settings).

Plain additive column — no RLS interaction (Codex review: ADD COLUMN is normal
DDL, runs under the migration/owner role; it does not alter any RLS predicate on
`users`). `JSON` (not JSONB) to match the ORM `Column(JSON)` type and avoid
compare_type churn under Alembic's compare_type=True. server_default '{}' with
NOT NULL backfills existing rows, so no separate UPDATE is needed.

Revision ID: 032
Revises: 031
Create Date: 2026-06-03
"""

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notification_prefs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notification_prefs")
