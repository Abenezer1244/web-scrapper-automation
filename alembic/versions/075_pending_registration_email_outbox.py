"""pending_registrations email outbox columns (075)

Makes the verification email durable + recoverable. The signup row in
pending_registrations IS the outbox: instead of the request path enqueueing a
fire-and-forget Celery task (which is lost if Redis — the Celery broker — is
down at signup time), the row is committed in Postgres and a beat dispatcher
(`dispatch_pending_verification_emails`) sends it and records the outcome HERE.
A signup made during a Redis outage is therefore drained and sent once Redis
recovers, never silently lost.

Columns:
  * email_dispatch_state — 'pending' (needs sending) | 'sent' (provider accepted
    it) | 'suppressed' (a real send to this address happened <120s ago, so this
    duplicate attempt is dequeued WITHOUT sending — the email-bomb guard) |
    'failed' (permanent send error after the attempt cap; ops-alerted). Only
    'sent' rows count toward the bomb guard's "last real send" — 'suppressed'
    must NOT, or repeated rapid signups could indefinitely suppress every real
    email (Codex).
  * verification_email_sent_at — wall-clock of the provider-confirmed send;
    drives the per-address bomb-guard window. NULL until really sent.
  * email_attempts — transient-failure counter (INTEGER; capped in the worker).
  * next_email_attempt_at — dispatch lease / backoff: a row is eligible when
    state='pending' AND next_email_attempt_at <= now(). Defaults to now() so a
    fresh signup is picked up on the next beat tick (~60s).

A partial index on (next_email_attempt_at) WHERE state='pending' keeps the
dispatcher scan cheap as 'sent'/'suppressed'/'failed' rows accumulate before
purge.

`expires_at` semantics are UNCHANGED (verification deadline = signup + 24h; the
verify endpoint and the purge sweep both still key off it). The verification
token is now minted with exp == expires_at (not now()+24h) so a delayed send can
never produce a link that outlives — or under-lives — the row it redeems.

Additive + reversible: new nullable/defaulted columns + one index; downgrade
drops them. No backfill needed (existing rows, if any, default to 'pending' and
get picked up by the dispatcher).
"""
import sqlalchemy as sa
from alembic import op

revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_registrations",
        sa.Column(
            "email_dispatch_state",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "pending_registrations",
        sa.Column("verification_email_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pending_registrations",
        sa.Column(
            "email_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "pending_registrations",
        sa.Column(
            "next_email_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Dispatcher scan: due, still-unsent rows only. Partial WHERE state='pending'
    # so the index stays small as terminal-state rows pile up before purge.
    op.create_index(
        "ix_pending_registrations_dispatch_due",
        "pending_registrations",
        ["next_email_attempt_at"],
        postgresql_where=sa.text("email_dispatch_state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_registrations_dispatch_due",
        table_name="pending_registrations",
    )
    op.drop_column("pending_registrations", "next_email_attempt_at")
    op.drop_column("pending_registrations", "email_attempts")
    op.drop_column("pending_registrations", "verification_email_sent_at")
    op.drop_column("pending_registrations", "email_dispatch_state")
