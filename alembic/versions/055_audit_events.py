"""M7: durable audit_events table. (055)

audit_log() was file/console-only — lost on container restart, unqueryable for
incident response. Additive table; writes are best-effort from the API (a
failed audit insert never fails the request — the log line remains).

user_id intentionally carries NO foreign key: audit rows must survive user
deletion (CASCADE would erase the trail, SET NULL would anonymize it).

Revision ID: 055
Revises: 054
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("user_id", UUID(as_uuid=False), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("path", sa.String(256), nullable=True),
        sa.Column("detail", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_events_event", "audit_events", ["event"])
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_event", table_name="audit_events")
    op.drop_table("audit_events")
