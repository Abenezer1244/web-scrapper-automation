"""scraper_configs.include_living_owner_tod for probate TOD toggle (072)

Renumbered 071 -> 072: PR #117 (this migration) and PR #118 (users.name) both
merged to main as revision 071 off 070 — different filenames, so git/GitHub saw
no conflict, but Alembic rejects the duplicate revision id. #118 merged first and
keeps 071; this one moves to 072 chained on 071. Both are independent additive
column adds, so the order is functionally irrelevant.

Tri-state nullable boolean controlling whether LIVING-owner Transfer-on-Death
deeds are included in a probate scraper's delivered leads:
  NULL  = legacy / grandfathered -> include (Phase 2 already labels the subtype)
  False = new probate default     -> exclude living-owner TOD planning docs
  True  = explicit customer opt-in -> include

Additive; existing rows default NULL (grandfathered — no behavior change). The API
writes False for newly created probate configs; the worker enforces the filter.
"""
import sqlalchemy as sa
from alembic import op

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_configs",
        sa.Column("include_living_owner_tod", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraper_configs", "include_living_owner_tod")
