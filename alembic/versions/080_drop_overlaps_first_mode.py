"""Drop the dead 'overlaps_first' delivery mode (080)

`overlaps_first` was a reserved delivery_mode that produced output IDENTICAL to
`everything` (the combined export already ranks overlaps first) and was never
exposed in the UI — a public API/CHECK trap. Removing it:

  1. Rewrite any stray rows to 'everything' (output-identical, so behavior is
     unchanged) BEFORE tightening the CHECK, so the new constraint can't fail on
     existing data. New batches never used it (the wizard only ever sent
     overlaps_only/everything).
  2. Recreate the CHECK allowing only ('overlaps_only', 'everything').

ROLLOUT ORDER (Codex P2): deploy the narrowed API FIRST, then run this migration.
This is the REVERSE of the usual "migrate before deploy" rule (which is about
additive columns the ORM selects) because this is a constraint TIGHTENING. If the
CHECK is tightened while old API workers are still live, an old worker that accepts
'overlaps_first' would hit the new CHECK and 500. Deploying the narrowed Literal
first means the value is rejected with a clean 422 at the API boundary; once old
workers are drained, tightening the CHECK is a safe no-op (no writer emits it).
In practice the wizard never sent 'overlaps_first', so no client traffic carries it
either way — this ordering just removes the theoretical 500 window.
"""
from alembic import op

revision = "080"
down_revision = "079"
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
