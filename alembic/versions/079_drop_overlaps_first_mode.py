"""Drop the dead 'overlaps_first' delivery mode (079)

`overlaps_first` was a reserved delivery_mode that produced output IDENTICAL to
`everything` (the combined export already ranks overlaps first) and was never
exposed in the UI — a public API/CHECK trap. Removing it:

  1. Rewrite any stray rows to 'everything' (output-identical, so behavior is
     unchanged) BEFORE tightening the CHECK, so the new constraint can't fail on
     existing data. New batches never used it (the wizard only ever sent
     overlaps_only/everything).
  2. Recreate the CHECK allowing only ('overlaps_only', 'everything').

Migrate BEFORE deploying the API that narrows the Pydantic Literal — an old
client sending 'overlaps_first' then gets a clean 422 instead of a DB violation.
"""
from alembic import op

revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None

_CK = "ck_scraper_batches_delivery_mode"


def upgrade() -> None:
    op.execute(
        "UPDATE scraper_batches SET delivery_mode = 'everything' "
        "WHERE delivery_mode = 'overlaps_first'"
    )
    op.drop_constraint(_CK, "scraper_batches", type_="check")
    op.create_check_constraint(
        _CK, "scraper_batches",
        "delivery_mode IN ('overlaps_only', 'everything')",
    )


def downgrade() -> None:
    op.drop_constraint(_CK, "scraper_batches", type_="check")
    op.create_check_constraint(
        _CK, "scraper_batches",
        "delivery_mode IN ('overlaps_only', 'overlaps_first', 'everything')",
    )
