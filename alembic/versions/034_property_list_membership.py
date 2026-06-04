"""Add property_list_membership (034) — Phase 1 cross-list overlap rollup.

Schema only. NO data backfill here: scripts/migrate.py runs migrations under a
~900s advisory lock on API boot, and results can be ~hundreds of millions of
rows; a backfill in-migration would brick the deploy. Historical seeding lives
in scripts/backfill_property_membership.py (offline, best-effort, idempotent).

RLS: ENABLE + a per-tenant USING policy (migration 018 pattern) — reads isolate
by app.current_user_id. This table is APP-READABLE (Phase 3 reads overlap from
the API), so role GRANTs are modeled on `results`, not worker-only
delivered_records; those grants live in the operator RLS scripts, not here.
FORCE is applied out-of-band by scripts/apply_rls_force.sql.

Revision ID: 034
Revises: 033
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "property_list_membership",
        sa.Column("user_id", UUID(as_uuid=False), nullable=False),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("property_key", sa.String(length=64), nullable=False),
        sa.Column("parcel_id", sa.String(length=64), nullable=True),
        sa.Column("property_address", sa.String(length=512), nullable=True),
        sa.Column("sighting_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "record_type", "property_key"),
    )
    op.create_index(
        "ix_property_list_membership_user_key",
        "property_list_membership",
        ["user_id", "property_key"],
    )
    op.execute("ALTER TABLE property_list_membership ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY property_list_membership_user_isolation
        ON property_list_membership
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS property_list_membership_user_isolation "
        "ON property_list_membership"
    )
    op.drop_index("ix_property_list_membership_user_key", table_name="property_list_membership")
    op.drop_table("property_list_membership")
