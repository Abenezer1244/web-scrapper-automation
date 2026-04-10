"""Sprint 4 Phase 1: skip trace schema (Tracerfy).

Adds:
- `results.phone`, `phone_type`, `phone_dnc_flag`, `email`, `skip_trace_status`,
  `skip_trace_attempted_at` — per-record skip trace outcome columns.
- `scraper_configs.skip_trace_enabled` — user opt-in flag (default False).
- `skip_trace_cache` — 90-day address-keyed cache so re-scraping the same
  property across daily jobs does not re-bill the user.
- `skip_trace_queues` — tracks Tracerfy batch queue IDs so webhook deliveries
  can be correlated back to the originating BridgeLeads job.
- `pending_skip_trace_rows` — work queue for the skip-trace dispatcher.
  Scrape jobs insert rows; the dispatcher (Celery Beat, every 5 min) drains
  and submits batches to Tracerfy within the 10/5-min rate limit.

Revision ID: 014
Revises: 013
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── results: skip trace outcome columns ──────────────────────────────────
    op.add_column("results", sa.Column("phone", sa.String(32), nullable=True))
    op.add_column("results", sa.Column("phone_type", sa.String(16), nullable=True))
    op.add_column("results", sa.Column("phone_dnc_flag", sa.Boolean(), nullable=True))
    op.add_column("results", sa.Column("email", sa.String(255), nullable=True))
    op.add_column(
        "results",
        sa.Column(
            "skip_trace_status",
            sa.String(16),
            nullable=False,
            server_default="not_attempted",
        ),
    )
    op.add_column(
        "results",
        sa.Column("skip_trace_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── scraper_configs: user opt-in ─────────────────────────────────────────
    op.add_column(
        "scraper_configs",
        sa.Column(
            "skip_trace_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ── skip_trace_cache: 90-day address-keyed cache ─────────────────────────
    op.create_table(
        "skip_trace_cache",
        sa.Column("address_hash", sa.String(64), primary_key=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("phone_type", sa.String(16), nullable=True),
        sa.Column("phone_dnc_flag", sa.Boolean(), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("raw_response", JSONB(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_skip_trace_cache_fetched_at",
        "skip_trace_cache",
        ["fetched_at"],
    )

    # ── skip_trace_queues: correlates Tracerfy queue_id ↔ our job_id ─────────
    op.create_table(
        "skip_trace_queues",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tracerfy_queue_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=False),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trace_type", sa.String(16), nullable=False, server_default="normal"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),  # pending | completed | errored
        sa.Column("rows_uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits_deducted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_skip_trace_queues_job_id", "skip_trace_queues", ["job_id"])
    op.create_index("ix_skip_trace_queues_status", "skip_trace_queues", ["status"])

    # ── pending_skip_trace_rows: dispatcher work queue ───────────────────────
    op.create_table(
        "pending_skip_trace_rows",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=False),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "result_id",
            UUID(as_uuid=False),
            sa.ForeignKey("results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Canonical address fields submitted to Tracerfy. Stored so the
        # dispatcher can build the batch payload without re-reading Result.
        sa.Column("property_address", sa.String(512), nullable=False),
        sa.Column("city", sa.String(128), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("zip", sa.String(16), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("last_name", sa.String(128), nullable=True),
        sa.Column("mail_address", sa.String(512), nullable=True),
        sa.Column("mail_city", sa.String(128), nullable=True),
        sa.Column("mail_state", sa.String(2), nullable=True),
        sa.Column("mail_zip", sa.String(16), nullable=True),
        # Dispatcher routing
        sa.Column(
            "trace_type",
            sa.String(16),
            nullable=False,
            server_default="normal",
        ),  # normal | advanced
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="queued",
        ),  # queued | submitted | completed | errored
        sa.Column(
            "tracerfy_queue_id",
            sa.Integer(),
            nullable=True,
        ),  # set when dispatcher submits the batch
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pending_skip_trace_rows_status",
        "pending_skip_trace_rows",
        ["status"],
    )
    op.create_index(
        "ix_pending_skip_trace_rows_job_id",
        "pending_skip_trace_rows",
        ["job_id"],
    )
    op.create_index(
        "ix_pending_skip_trace_rows_tracerfy_queue_id",
        "pending_skip_trace_rows",
        ["tracerfy_queue_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_skip_trace_rows_tracerfy_queue_id", table_name="pending_skip_trace_rows")
    op.drop_index("ix_pending_skip_trace_rows_job_id", table_name="pending_skip_trace_rows")
    op.drop_index("ix_pending_skip_trace_rows_status", table_name="pending_skip_trace_rows")
    op.drop_table("pending_skip_trace_rows")

    op.drop_index("ix_skip_trace_queues_status", table_name="skip_trace_queues")
    op.drop_index("ix_skip_trace_queues_job_id", table_name="skip_trace_queues")
    op.drop_table("skip_trace_queues")

    op.drop_index("ix_skip_trace_cache_fetched_at", table_name="skip_trace_cache")
    op.drop_table("skip_trace_cache")

    op.drop_column("scraper_configs", "skip_trace_enabled")

    op.drop_column("results", "skip_trace_attempted_at")
    op.drop_column("results", "skip_trace_status")
    op.drop_column("results", "email")
    op.drop_column("results", "phone_dnc_flag")
    op.drop_column("results", "phone_type")
    op.drop_column("results", "phone")
