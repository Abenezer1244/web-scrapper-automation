"""Add multi-contact columns: results + skip_trace_cache phones/emails (042).

Surfaces up to 3 phones + 3 emails per lead (Tracerfy already returns up to
5 mobiles / 3 landlines / 5 emails per hit; we previously kept only the single
best). The scalar results.phone/phone_type/email stay the PRIMARY value
(= phones[0]/emails[0]) so every existing consumer — dialer push, CSV export,
Lists/segments, skip-trace cache + dedup-reuse — is unchanged.

Additive, nullable JSON, NO server_default and NO backfill (migrations run under
an advisory lock on API boot; results can be large). NULL = legacy/not-yet-traced
row; [] = trace ran and found none. No index — these are display-only, never
filtered/joined. Forward population happens in workers (tracerfy_ingest +
tasks reuse/cache-hit).

Revision ID: 042
Revises: 041
Create Date: 2026-06-07
"""
import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("phones", sa.JSON(), nullable=True))
    op.add_column("results", sa.Column("emails", sa.JSON(), nullable=True))
    op.add_column("skip_trace_cache", sa.Column("phones", sa.JSON(), nullable=True))
    op.add_column("skip_trace_cache", sa.Column("emails", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("skip_trace_cache", "emails")
    op.drop_column("skip_trace_cache", "phones")
    op.drop_column("results", "emails")
    op.drop_column("results", "phones")
