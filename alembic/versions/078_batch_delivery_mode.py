"""scraper_batches.delivery_mode + batch_runs.delivery_counts (078)

Batch overlaps-first delivery (spec 2026-07-01): a per-batch delivery mode for
the combined export/leads view, and honest per-run delivery counts.

delivery_mode: 'overlaps_only' | 'overlaps_first' | 'everything'. Existing rows
backfill to 'everything' (server_default) so recurring scheduled batches keep
their current output on deploy; NEW batches default to 'overlaps_only' at the
API layer. CHECK constraint because scraper_batches is also written outside the
API (tests/scheduler) and bad data must fail early.

delivery_counts: JSON snapshot written by the worker at finalize
({leads_total, overlaps_delivered, singletons_suppressed, unmatchable_no_parcel}).
Additive + nullable — no backfill required.
"""
import sqlalchemy as sa
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_batches",
        sa.Column(
            "delivery_mode", sa.String(length=16),
            nullable=False, server_default="everything",
        ),
    )
    op.create_check_constraint(
        "ck_scraper_batches_delivery_mode",
        "scraper_batches",
        "delivery_mode IN ('overlaps_only', 'overlaps_first', 'everything')",
    )
    op.add_column("batch_runs", sa.Column("delivery_counts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("batch_runs", "delivery_counts")
    op.drop_constraint(
        "ck_scraper_batches_delivery_mode", "scraper_batches", type_="check"
    )
    op.drop_column("scraper_batches", "delivery_mode")
