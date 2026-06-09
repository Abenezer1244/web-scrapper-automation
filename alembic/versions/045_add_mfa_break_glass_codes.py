"""Add mfa_break_glass_codes table (045).

H2 Phase 5 (admin MFA enforcement) — break-glass recovery. Operator-issued
emergency MFA codes, redeemed at /auth/login/break-glass when an account holder
loses their authenticator. SEPARATE from mfa_backup_codes: tracks who issued the
code, why, an optional expiry, and the redemption ip/user-agent for a loud audit
trail.

Like mfa_backup_codes (043) this is a SYSTEM/worker-accessed table: the redeem
happens during login, BEFORE the user is authenticated, so the RLS GUC
(app.current_user_id) is not set then — those ops run under the system role with
a MANDATORY explicit user_id filter. RLS is ENABLED + a per-tenant USING policy
as the belt (advisory today under the BYPASSRLS system role / RLS_ENFORCE=False).
The system role already has SELECT/INSERT/UPDATE on ALL TABLES (re-applied at
provisioning), which covers the operator scripts. The REQUEST path also writes
this table (break-glass redeem UPDATEs it under bridgeleads_app at the RLS-enforce
cutover), so bridgeleads_app will need SELECT/UPDATE here at H1 — tracked as an
H1-CUTOVER TODO in scripts/provision_rls_roles.sql (not added now: it must be
reconciled with that script's "no app DELETE" least-privilege invariant, and
RLS_ENFORCE=False today so nothing is gated yet).

Additive (new table only), so it backfills nothing. Runs under the advisory-lock
runner (scripts/migrate.py) on boot.

Revision ID: 045
Revises: 044
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mfa_break_glass_codes",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("batch_id", UUID(as_uuid=False), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_reason", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(length=64), nullable=True),
        sa.Column("used_user_agent", sa.String(length=500), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mfa_break_glass_codes_user_id", "mfa_break_glass_codes", ["user_id"])
    # RLS belt (mirrors mfa_backup_codes / 043): enable + per-tenant USING policy.
    # Query-layer explicit user_id filter is the suspenders.
    op.execute("ALTER TABLE mfa_break_glass_codes ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY mfa_break_glass_codes_user_isolation
        ON mfa_break_glass_codes
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS mfa_break_glass_codes_user_isolation ON mfa_break_glass_codes"
    )
    op.drop_index("ix_mfa_break_glass_codes_user_id", table_name="mfa_break_glass_codes")
    op.drop_table("mfa_break_glass_codes")
