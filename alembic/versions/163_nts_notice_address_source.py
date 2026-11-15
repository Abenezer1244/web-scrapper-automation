"""nts_notices: where property_address came from, and the address the notice printed.

A trustee's "more commonly known as" address can be years out of date: Chelan
REC-2628915 printed 170 Hill Street, while the county assessor and the WA statewide
parcel layer give the same parcel (same legal description) as 62 Village Vista Ln. For
a recorder notice whose parcel is the recording's own APN, the operator-approved notice
now takes the parcel's current site address at approval and keeps the printed one.

  address_source     'assessor_situs' (the parcel's current site address, looked up at
                     approval and frozen) or 'notice' (the address the notice printed).
                     NULL on every row written before this, and on every other source.
  address_on_notice  the address exactly as the notice printed it, kept when
                     property_address is the assessor's.

Nullable, no default: existing rows and other sources are unaffected.

Revision ID: 163
Revises: 162
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from alembic import op

revision = "163"
down_revision = "162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nts_notices", sa.Column("address_source", sa.String(32), nullable=True))
    op.add_column("nts_notices", sa.Column("address_on_notice", sa.String(512), nullable=True))
    op.create_check_constraint(
        "ck_nts_notices_address_source", "nts_notices",
        "address_source IS NULL OR address_source IN ('assessor_situs', 'notice')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_nts_notices_address_source", "nts_notices", type_="check")
    op.drop_column("nts_notices", "address_on_notice")
    op.drop_column("nts_notices", "address_source")
