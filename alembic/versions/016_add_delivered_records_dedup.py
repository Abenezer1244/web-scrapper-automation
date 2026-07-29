"""Sprint 6.4: cross-job lead deduplication.

Adds:
- results.is_duplicate boolean (default false) — flagged when a record
  has been delivered to this user in a prior scrape.
- results.dedup_hash string(64) — sha256 of the normalized
  (property_address, parcel_id) tuple. Computed at insert time.
- delivered_records table — append-only log of (user_id, dedup_hash)
  pairs with a unique constraint so the same lead is never "delivered"
  twice to the same user.

The workers.tasks run_scrape_job flow is updated in a separate commit
to: (a) compute dedup_hash per Result, (b) upsert into delivered_records,
(c) if the upsert conflicts on the unique constraint, mark the Result
as is_duplicate=true, (d) exclude is_duplicate rows from records_used
monthly counting.

Revision ID: 016
Revises: 015
Create Date: 2026-04-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── results: dedup columns ───────────────────────────────────────────
    op.add_column(
        "results",
        sa.Column("dedup_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "results",
        sa.Column(
            "is_duplicate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_results_dedup_hash",
        "results",
        ["dedup_hash"],
    )

    # ── delivered_records table ──────────────────────────────────────────
    # Append-only log. Unique constraint on (user_id, dedup_hash) so we
    # can detect re-delivery via ON CONFLICT. First_result_id points at
    # the original Result row so the UI can link back to the previous
    # delivery if a user asks "where did I first see this lead?".
    op.create_table(
        "delivered_records",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dedup_hash", sa.String(64), nullable=False),
        sa.Column(
            "first_result_id",
            UUID(as_uuid=False),
            sa.ForeignKey("results.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("first_job_id", UUID(as_uuid=False), nullable=True),
        sa.Column("parcel_id", sa.String(64), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        sa.Column(
            "first_delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_delivered_records_user_hash",
        "delivered_records",
        ["user_id", "dedup_hash"],
    )
    op.create_index(
        "ix_delivered_records_user_id",
        "delivered_records",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_delivered_records_user_id", table_name="delivered_records")
    op.drop_constraint(
        "uq_delivered_records_user_hash",
        "delivered_records",
        type_="unique",
    )
    op.drop_table("delivered_records")

    op.drop_index("ix_results_dedup_hash", table_name="results")
    op.drop_column("results", "is_duplicate")
    op.drop_column("results", "dedup_hash")
