"""nts_notices.cancelled_at: a recorded Discontinuance cancelled this sale.

Benton auction notices come from the county recorder, which also records each
Discontinuance of Trustee's Sale. is_active alone cannot carry that: the expiry
pass flips it daily and the matcher's past-sale pass deliberately ignores it. A
separate, sticky timestamp lets every reader exclude a cancelled sale for good.
Nullable, no default: existing rows are unaffected.

Revision ID: 156
Revises: 155
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "156"
down_revision = "155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nts_notices", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("nts_notices", "cancelled_at")
