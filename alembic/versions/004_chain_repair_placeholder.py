"""Chain-repair placeholder for revision 004.

Migration 005_fix_county_urls.py was committed in March 2026 with
``down_revision = "004"``, but no `004_*.py` file was ever tracked in
git. Production databases were upgraded through that broken edge by
manually stamping past the missing revision, so the deployed schema
is fine — but `alembic history`, `alembic check`, and any fresh
deployment from `<base>` would error with:

    UserWarning: Revision 004 referenced from 004 -> 005 ... is not present

This placeholder satisfies the dangling reference without modifying
any existing migration. It performs no schema or data changes —
upgrade and downgrade are intentional no-ops.

Revision ID: 004
Revises: 003
Create Date: 2026-04-25 (chain-repair, retroactive)
"""

from alembic import op  # noqa: F401  -- imported for parity with other revisions

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
