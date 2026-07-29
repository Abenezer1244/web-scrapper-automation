"""Sprint 7.3: referral program.

Adds:
- users.referral_code (String(16), unique) — public shareable code
  generated at signup. URL format: ?ref=XXXXXXXX
- users.referred_by_user_id (UUID, FK users.id, nullable) — set when
  a user signs up via a referral link
- users.referral_credit_cents (Integer, default 0) — cumulative
  pending account credit in cents, grows when a referred user
  converts to paid
- referral_events table — append-only audit log of credit grants
  with a unique constraint on (referee_id) so each referred user can
  only trigger exactly one bonus

Does NOT touch Stripe customer balance. This migration only adds
the tracking schema; the billing route computes credit as a simple
display value. A later phase can layer Stripe customer-balance
application once the tracking is proven stable.

Revision ID: 017
Revises: 016
Create Date: 2026-04-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users: referral tracking columns ─────────────────────────────────
    op.add_column(
        "users",
        sa.Column("referral_code", sa.String(16), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "referred_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "referral_credit_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_users_referral_code",
        "users",
        ["referral_code"],
        unique=True,
    )
    op.create_index(
        "ix_users_referred_by_user_id",
        "users",
        ["referred_by_user_id"],
    )

    # ── referral_events table ────────────────────────────────────────────
    op.create_table(
        "referral_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "referrer_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "referee_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # One bonus per referee — idempotency guard for the webhook handler
    op.create_unique_constraint(
        "uq_referral_events_referee",
        "referral_events",
        ["referee_id"],
    )
    op.create_index(
        "ix_referral_events_referrer_id",
        "referral_events",
        ["referrer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_referral_events_referrer_id", table_name="referral_events")
    op.drop_constraint(
        "uq_referral_events_referee",
        "referral_events",
        type_="unique",
    )
    op.drop_table("referral_events")
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referral_credit_cents")
    op.drop_column("users", "referred_by_user_id")
    op.drop_column("users", "referral_code")
