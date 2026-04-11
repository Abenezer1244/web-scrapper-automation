"""Batch 3 C3: enforce uniqueness of users.stripe_customer_id.

Adds a partial unique index on users.stripe_customer_id WHERE
stripe_customer_id IS NOT NULL. The "IS NOT NULL" filter matters:
most users (starter / trial) have NULL customer_ids, and Postgres
treats NULL as distinct in a standard UNIQUE constraint — but the
partial index is the canonical pattern for "unique when present".

Why this matters: several billing webhook handlers
(_handle_subscription_updated, _handle_subscription_deleted,
_handle_payment_failed) look up the user by stripe_customer_id.
If two User rows ever share the same customer_id — whether via
account re-registration, a manual DB write, or a bug — the webhook
applies plan changes to whichever row Postgres returns first,
which can silently grant or revoke a plan on the wrong account.
A unique index makes the invariant enforceable.

Verified prior to shipping: SELECT stripe_customer_id, COUNT(*)
FROM users GROUP BY stripe_customer_id HAVING COUNT(*) > 1
returned 0 duplicates, so the index creation will not fail.

Revision ID: 019
Revises: 018
Create Date: 2026-04-11
"""

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_stripe_customer_id
        ON users (stripe_customer_id)
        WHERE stripe_customer_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_stripe_customer_id")
