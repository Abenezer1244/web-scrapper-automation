"""scraper_configs.include_living_owner_tod for probate TOD toggle (071)

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

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_configs",
        sa.Column("include_living_owner_tod", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraper_configs", "include_living_owner_tod")
