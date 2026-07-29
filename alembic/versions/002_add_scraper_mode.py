"""Add scraper_mode column to county_connectors

Revision ID: 002
Revises: 001
Create Date: 2026-03-18
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "county_connectors",
        sa.Column("scraper_mode", sa.String(16), nullable=False, server_default="manual"),
    )
    # Existing connectors stay as "manual", new ones default to "ai"
    op.alter_column("county_connectors", "scraper_mode", server_default="ai")


def downgrade() -> None:
    op.drop_column("county_connectors", "scraper_mode")
