"""Add MFA: users.mfa_* columns + mfa_backup_codes table (043).

H2 of the security checklist audit (2026-06-08). TOTP-based MFA, Phase 1:
schema + storage only — NO login-flow change in this migration.

- users.mfa_enabled (bool, server_default false → existing rows backfill safely)
- users.mfa_secret_encrypted (text) — Fernet token of the TOTP secret
  (src/utils/crypto.py), never the raw secret; nullable until enrolled
- users.mfa_enrolled_at (timestamptz, nullable)
- mfa_backup_codes table — one-time backup codes stored as HMAC-SHA256 hex
  digests, consumed atomically (used_at) on use

mfa_backup_codes is a SYSTEM/worker-accessed table (same posture as
dialer_deliveries / delivered_records): the backup-code VERIFY happens during
login, BEFORE the user is authenticated, so the RLS GUC (app.current_user_id)
is not set then — those ops run under the system role with a MANDATORY explicit
user_id filter. RLS is ENABLED + a per-tenant USING policy as the belt
(advisory today under the BYPASSRLS system role / RLS_ENFORCE=False). The system
role already has SELECT/INSERT/UPDATE on ALL TABLES, so no RLS-cutover grant
change is needed.

Additive + nullable/defaulted, so it backfills existing rows without a data
migration. Runs under the advisory-lock runner (scripts/migrate.py) on boot.

Revision ID: 043
Revises: 042
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("users", sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "mfa_backup_codes",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mfa_backup_codes_user_id", "mfa_backup_codes", ["user_id"])
    # RLS belt (mirrors dialer_deliveries / 041): enable + per-tenant USING
    # policy. Query-layer explicit user_id filter is the suspenders.
    op.execute("ALTER TABLE mfa_backup_codes ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mfa_backup_codes_user_isolation
        ON mfa_backup_codes
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS mfa_backup_codes_user_isolation ON mfa_backup_codes"
    )
    op.drop_index("ix_mfa_backup_codes_user_id", table_name="mfa_backup_codes")
    op.drop_table("mfa_backup_codes")
    op.drop_column("users", "mfa_enrolled_at")
    op.drop_column("users", "mfa_secret_encrypted")
    op.drop_column("users", "mfa_enabled")
