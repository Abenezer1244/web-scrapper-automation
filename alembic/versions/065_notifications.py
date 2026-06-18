"""Phase 2b: in-app notifications table + RLS. (065)

Mirrors the 056 pattern: ENABLE ROW LEVEL SECURITY + an untargeted per-tenant
GUC isolation policy, inline and role-INDEPENDENT (the 030 lesson). The
role-targeted _app/_system policies (and the DROP of the untargeted one) live
in scripts/apply_rls_cutover_policies.sql; FORCE lives in
scripts/apply_rls_force.sql.

Revision ID: 065
Revises: 064
Create Date: 2026-06-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID  # exact form used by migration 055

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("job_id", UUID(as_uuid=False), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    # Composite for the list query; partial for the unread-count badge.
    op.create_index(
        "ix_notifications_user_created", "notifications",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_notifications_user_unread", "notifications", ["user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    # RLS: enable + untargeted isolation policy (role-independent; inert under
    # today's BYPASSRLS runtime role, constrains any non-bypass role).
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY notifications_user_isolation
        ON notifications
        USING ({_GUC_PREDICATE})
        """
    )


def downgrade() -> None:
    # Mirrors 056: drop the policy, drop the table. (RLS goes away with the table.)
    op.execute("DROP POLICY IF EXISTS notifications_user_isolation ON notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
