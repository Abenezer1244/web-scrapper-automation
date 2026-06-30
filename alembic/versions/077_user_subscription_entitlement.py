"""users.stripe_subscription_id + subscription_status (077)

Durable Stripe ENTITLEMENT signal. `stripe_customer_id` only means "has touched
Stripe" — it is created when a user OPENS checkout, not when they pay. The hourly
`expire_trials` task used `stripe_customer_id IS NULL` as its "hasn't paid" gate,
so a trial user who opened checkout but never paid kept Pro forever after their
trial. These two columns record the authoritative subscription id + Stripe status
(populated by the billing webhooks); `expire_trials` now gates on an entitled
status ("active"/"trialing"/"past_due") instead of customer-id presence.

Additive + nullable; existing rows stay NULL and are populated by the next
relevant Stripe webhook (no backfill required — prod currently has no active paid
subscriber to protect).
"""
import sqlalchemy as sa
from alembic import op

revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("subscription_status", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "subscription_status")
    op.drop_column("users", "stripe_subscription_id")
