"""Add users.revoked_at for durable logout-all / revocation tracking.

Phase 0 of this branch made revocation a security boundary that fails
closed — every authenticated request consults Redis to read the user's
revoke timestamp, and any RedisError surfaces as 503. Codex adversarial
review (post-deploy) flagged this as an availability regression: a
Redis blip takes the entire authenticated API down even when the JWT,
DB, and app are otherwise healthy.

Persist the per-user revocation timestamp in the users table so JWT
validation has a durable source of truth that survives Redis outages.
Redis stays as a hot cache (microsecond reads); DB is the fallback
when Redis raises. The per-jti blacklist in src/api/middleware/
auth_hardening.py:TokenBlacklist.add stays Redis-only because logout
of a single token is rare and the blast radius of fail-open during a
Redis outage is bounded by the JWT's natural expiry.

Revision ID: 024
Revises: 023
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "revoked_at")
