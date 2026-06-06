"""Add dialer_deliveries (041) — Thread 3 per-contact dialer outbox.

Durable per-contact delivery state for native dialer connectors (PhoneBurner has
no bulk endpoint → 500 POSTs → partial success). Lets a replay re-send ONLY the
non-'delivered' rows. Materialized by dialer_push_sweep, drained by
process_dialer_outbox, both under the system role.

WORKER/SYSTEM-ONLY table (like delivered_records): NOT granted to bridgeleads_app.
The system role already has SELECT/INSERT/UPDATE on ALL TABLES, so no RLS-cutover
grant-script change is needed. Tenant isolation is enforced by mandatory explicit
user_id filters in every query (the sweep's owner-match + the processor/replay
filters). RLS is ENABLED + a per-tenant USING policy as the belt (advisory today
under the BYPASSRLS system role / RLS_ENFORCE=False); its system-escape at
enforcement time is governed by the operator RLS scripts, not this migration
(same posture as the other worker tables).

Schema only — no backfill. Credentials are NEVER stored here.

Revision ID: 041
Revises: 040
Create Date: 2026-06-05
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dialer_deliveries",
        sa.Column("id", UUID(as_uuid=False), nullable=False),
        sa.Column("job_id", UUID(as_uuid=False), nullable=False),
        sa.Column("result_id", UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", UUID(as_uuid=False), nullable=False),
        sa.Column("scraper_config_id", UUID(as_uuid=False), nullable=False),
        sa.Column("vendor_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.Column("vendor_response_code", sa.Integer(), nullable=True),
        sa.Column("vendor_contact_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["result_id"], ["results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scraper_config_id"], ["scraper_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "result_id", name="uq_dialer_delivery_job_result"),
    )
    op.create_index("ix_dialer_deliveries_job_id", "dialer_deliveries", ["job_id"])
    op.create_index("ix_dialer_deliveries_status", "dialer_deliveries", ["status"])
    op.create_index(
        "ix_dialer_deliveries_job_status", "dialer_deliveries", ["job_id", "status"]
    )
    op.execute("ALTER TABLE dialer_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY dialer_deliveries_user_isolation
        ON dialer_deliveries
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS dialer_deliveries_user_isolation ON dialer_deliveries"
    )
    op.drop_index("ix_dialer_deliveries_job_status", table_name="dialer_deliveries")
    op.drop_index("ix_dialer_deliveries_status", table_name="dialer_deliveries")
    op.drop_index("ix_dialer_deliveries_job_id", table_name="dialer_deliveries")
    op.drop_table("dialer_deliveries")
