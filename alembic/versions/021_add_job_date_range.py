"""Add date_from and date_to columns to jobs table.

Stores the resolved date range that was actually scraped, so the
frontend can show "scraped Apr 14 – Apr 14" instead of a generic
"no new records" message.

Revision ID: 021
Revises: 020
Create Date: 2026-04-14
"""

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("date_from", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("date_to", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "date_to")
    op.drop_column("jobs", "date_from")
