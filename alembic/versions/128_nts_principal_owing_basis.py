"""nts_notices.principal_owing_basis: what the stored Section IV amount is (128).

The parser prefers the Section IV figure labelled "principal"; when the section has
no such label (a matured loan: "sum owing on the matured obligation ... is: $X") it
takes the first figure, which is the total owing, not principal. Both shipped as
"Principal Owing". Owner decision 2026-11-06: keep those amounts, mark them.

'principal' | 'sum_owing'; NULL = parsed before this column existed (unknown), and
is never presented as principal. Additive, nullable; the running code never reads it
before this lands. No backfill here: the source text is not stored, so active
notices gain a basis as the crawlers re-read them.
"""
import sqlalchemy as sa
from alembic import op

revision = "128"
down_revision = "127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nts_notices", sa.Column("principal_owing_basis", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_nts_notices_principal_owing_basis",
        "nts_notices",
        "principal_owing_basis IN ('principal', 'sum_owing')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_nts_notices_principal_owing_basis", "nts_notices", type_="check")
    op.drop_column("nts_notices", "principal_owing_basis")
