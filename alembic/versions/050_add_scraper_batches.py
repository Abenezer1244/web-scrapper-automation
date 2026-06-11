"""Add scraper_batches + batch_runs + scraper_configs.batch_id (Piece 2: batch scrape). (050)

A 'batch scrape' is a parent (scraper_batches) holding shared
fields/enrichment/schedule/deliver, fanned out into N child scraper_configs
(one per county x record_type) via the nullable scraper_configs.batch_id. Each
execution is a batch_runs row (system-written, like dialer_deliveries) whose
completion barrier triggers one combined CSV + one delivery.

TENANT INTEGRITY (Codex P1): scraper_batches has UNIQUE(id, user_id); both
batch_runs and scraper_configs reference it via a COMPOSITE FK (batch_id,
user_id) -> (id, user_id), so a child config / run can never point at another
tenant's batch. MATCH SIMPLE => the check is skipped when batch_id IS NULL (an
ordinary single scrape). ondelete CASCADE: NEVER hard-delete a scraper_batches
row (archive via status) — it would cascade to child configs + their jobs/results.

Single scrapes are unaffected (batch_id NULL; no batch rows).

Revision ID: 050
Revises: 049
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "scraper_batches",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("enrichment", sa.JSON(), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("deliver", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("id", "user_id", name="uq_scraper_batches_id_user"),
    )
    op.create_index("ix_scraper_batches_user_id", "scraper_batches", ["user_id"])

    op.create_table(
        "batch_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("batch_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("child_job_ids", sa.JSON(), nullable=False),
        sa.Column("combined_export_key", sa.String(512), nullable=True),
        sa.Column("excluded_no_date_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_children", sa.JSON(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id", "user_id"],
            ["scraper_batches.id", "scraper_batches.user_id"],
            ondelete="CASCADE",
            name="fk_batch_runs_batch_tenant",
        ),
        # ONE run per batch for the on-demand MVP (Phase 2A). Makes the worker
        # fan-out at-most-once + race-safe: a duplicate Celery delivery hits this
        # on flush -> IntegrityError -> the loser rolls back (Codex P1). NOTE: a
        # scheduled batch (Phase 2B) has many runs per batch and MUST revisit this.
        sa.UniqueConstraint("batch_id", name="uq_batch_runs_batch_id"),
    )
    op.create_index("ix_batch_runs_batch_id", "batch_runs", ["batch_id"])
    op.create_index("ix_batch_runs_user_id", "batch_runs", ["user_id"])

    op.add_column("scraper_configs", sa.Column("batch_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_scraper_configs_batch_tenant",
        "scraper_configs",
        "scraper_batches",
        ["batch_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_scraper_configs_batch_id", "scraper_configs", ["batch_id"])


def downgrade() -> None:
    # scraper_configs references scraper_batches -> drop its FK + column first.
    op.drop_index("ix_scraper_configs_batch_id", table_name="scraper_configs")
    op.drop_constraint("fk_scraper_configs_batch_tenant", "scraper_configs", type_="foreignkey")
    op.drop_column("scraper_configs", "batch_id")
    op.drop_table("batch_runs")       # drops its indexes + composite FK
    op.drop_table("scraper_batches")  # drops its unique constraint + index
