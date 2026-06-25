"""pending_registrations — staging table for email-verified signups (074)

Backs the enumeration-safe registration flow (EMAIL_VERIFICATION_ENABLED). A
signup is stored HERE, not as a real users row, until the emailed verification
link is redeemed — which is what closes the account-squatting hole (an attacker
can no longer pre-create a real account for someone else's address with their
own password/trial). On verify the row is consumed and the real users row is
created; unredeemed rows expire via expires_at and are purged.

email / first_name / last_name are encrypted at rest (EncryptedString -> Text),
exactly like users.* ; the searchable key is email_hmac (the keyed HMAC blind
index), never the encrypted email.

email_hmac is INDEXED but NOT unique on purpose: each registration attempt
inserts its OWN independent pending row. Collapsing attempts into one row (an
upsert) enables account pre-hijacking — an attacker could submit a victim's
address and overwrite the password on the victim's still-pending row, so the
victim's verification link would then activate the attacker's password. Separate
rows mean the attacker's submission lands in a SEPARATE row whose link is emailed
to the victim's inbox (which the attacker does not control). First verification
wins via UNIQUE(users.email_hmac); siblings are dropped at verify time, and
unredeemed rows expire via expires_at and are purged.

Additive + reversible: no users table change; downgrade drops the table.
"""
import sqlalchemy as sa
from alembic import op

revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_registrations",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True),
        # Keyed HMAC blind index of the normalized email — the searchable lookup
        # key (the email column itself is encrypted, non-searchable). INDEXED but
        # NOT unique: each attempt gets its own row (see module docstring — this
        # is what prevents account pre-hijacking).
        sa.Column("email_hmac", sa.String(64), nullable=False),
        # Encrypted at rest (app-layer EncryptedString), stored as Text.
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        # NOTE: no password column. The password is set at /auth/verify-email by
        # whoever proves email control, never carried from the (unverified)
        # register step — that is what prevents account pre-hijacking.
        # Raw (validated) referral code, resolved to a referrer at verify time so
        # a referrer who deactivates between signup and verify is handled then.
        sa.Column("ref_code", sa.String(16), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Sibling-cleanup at verify (delete all pending rows for an email) + general
    # lookups scan by email_hmac. NOT unique (separate row per attempt).
    op.create_index(
        "ix_pending_registrations_email_hmac",
        "pending_registrations",
        ["email_hmac"],
    )
    # Purge query (delete expired) scans by expires_at.
    op.create_index(
        "ix_pending_registrations_expires_at",
        "pending_registrations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_registrations_expires_at", table_name="pending_registrations")
    op.drop_index("ix_pending_registrations_email_hmac", table_name="pending_registrations")
    op.drop_table("pending_registrations")
