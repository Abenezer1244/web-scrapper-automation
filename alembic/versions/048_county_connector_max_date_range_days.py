"""Add county_connectors.max_date_range_days (model/migration drift fix). (048)

The CountyConnector model has had `max_date_range_days` (Integer, nullable) for a
while, but no migration ever created it — prod got the column via a manual ALTER,
so a fresh database (CI, or a new environment) lacks it and every registry lookup
fails with "column county_connectors.max_date_range_days does not exist". This
migration makes the schema reproducible.

ADD COLUMN IF NOT EXISTS: idempotent and safe on prod (where the column already
exists from the manual ALTER) — it becomes a no-op there.

Revision ID: 048
Revises: 047
Create Date: 2026-06-10
"""
from alembic import op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE county_connectors "
        "ADD COLUMN IF NOT EXISTS max_date_range_days INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE county_connectors DROP COLUMN IF EXISTS max_date_range_days")
