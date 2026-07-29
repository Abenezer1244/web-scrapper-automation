"""Add results.doc_type (035) — Phase 2a surface pre-foreclosure document type.

Schema only, additive, nullable. Old rows stay NULL (CountyRecord.doc_type is a
separate fuzzy-keyed path — a bad backfill is worse than null). Forward-only:
the worker populates it going forward.

Revision ID: 035
Revises: 034
Create Date: 2026-06-04
"""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("doc_type", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("results", "doc_type")
