"""Add scraper_configs.doc_types (036) — Phase 2b pre-foreclosure doc-type selection.

Schema only, additive, nullable JSON. NULL = legacy behavior (today's full
output); a non-empty list narrows to the user-chosen canonical document types
(validated in the route against src/scrapers/doc_types.py). No backfill — every
existing config keeps NULL = unchanged behavior.

Revision ID: 036
Revises: 035
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scraper_configs", sa.Column("doc_types", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("scraper_configs", "doc_types")
