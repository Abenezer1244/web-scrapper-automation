"""Tier 0: owner-location flags on results (absentee / out-of-state). (057)

Adds four NULLABLE columns derived from the property (situs) + mailing addresses
we already store: property_state, owner_state (mailing state), absentee_owner,
out_of_state_owner. Computed in Python by src/utils/address_intel.compute_owner_flags
(NOT a generated column — address parsing is too business-rule-heavy for an
IMMUTABLE SQL expression; Codex). The worker populates them at insert and at the
end-of-job recompute after enrichment fills the mailing address; existing 310k
rows are backfilled out-of-band by scripts/backfill_owner_flags.py.

All columns NULLABLE = an instant catalog-only ALTER (no table rewrite), safe on
the live RLS-FORCE'd results table. NO indexes here (Codex): they'd take a
blocking build on 310k rows inside Alembic's transaction. Partial indexes for the
bool filters are added out-of-band (CREATE INDEX CONCURRENTLY) when the filters
ship. Booleans are nullable tri-state: True/False/NULL(unknown).

Revision ID: 057
Revises: 056
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("property_state", sa.String(length=2), nullable=True))
    op.add_column("results", sa.Column("owner_state", sa.String(length=2), nullable=True))
    op.add_column("results", sa.Column("absentee_owner", sa.Boolean(), nullable=True))
    op.add_column("results", sa.Column("out_of_state_owner", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("results", "out_of_state_owner")
    op.drop_column("results", "absentee_owner")
    op.drop_column("results", "owner_state")
    op.drop_column("results", "property_state")
