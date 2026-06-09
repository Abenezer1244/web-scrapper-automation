"""H3 Phase 4: add users.email_hmac blind-index column (nullable). (047)

Additive + safe: a new nullable, indexed column. NOT unique yet and NOT read by
any lookup at this stage — the app only dual-writes it (User.@validates) so new
and updated users get their blind index. Existing rows are populated out of band
by scripts/backfill_user_email_hmac.py.

The UNIQUE + NOT NULL promotion and the login-lookup cutover happen in P5
(migration 048), only AFTER the backfill confirms every row is populated — doing
it here would lock out existing users (NULL email_hmac) the moment the new code
reads by it.

Revision ID: 047
Revises: 046
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_hmac", sa.String(length=64), nullable=True))
    op.create_index("ix_users_email_hmac", "users", ["email_hmac"])


def downgrade() -> None:
    op.drop_index("ix_users_email_hmac", table_name="users")
    op.drop_column("users", "email_hmac")
