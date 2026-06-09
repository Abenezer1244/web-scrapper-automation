"""Add users.mfa_last_totp_counter for TOTP replay prevention (044).

H2 Phase 4 (session hardening). pyotp accepts a TOTP for a ±1 step (~90s)
window, so without tracking, a single intercepted code could be replayed within
that window. This column records the highest 30s timestep counter already
consumed for a user; login verification advances it atomically
(`... WHERE mfa_last_totp_counter IS NULL OR < :counter`) so a code can be used
exactly once.

Additive + nullable (NULL = no TOTP login consumed yet), so it backfills
existing rows without a data migration. Runs under the advisory-lock runner
(scripts/migrate.py) on boot.

Revision ID: 044
Revises: 043
Create Date: 2026-06-08
"""
import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("mfa_last_totp_counter", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "mfa_last_totp_counter")
