"""H3: widen/convert contact-PII columns to TEXT for at-rest encryption (046).

Phase 2 of H3. The in-scope contact-PII columns become Fernet ciphertext
(``fe1:``-prefixed tokens) which far exceed the old ``VARCHAR(32)``/``(255)``
widths, and the JSON multi-contact / raw-payload columns must hold a ciphertext
*string*. This migration only changes column TYPES — it does NOT encrypt data.
Backfill is an out-of-band script (Phase 3); reads stay correct meanwhile
because ``decrypt_field`` is tolerant (legacy plaintext / JSON-text passes
through).

Scope (private contact PII only — names/addresses stay plaintext, they are
public-record and ILIKE-searched):
  results:          phone, email (VARCHAR->TEXT); phones, emails (JSON->TEXT)
  skip_trace_cache: phone, email (VARCHAR->TEXT); phones, emails, raw_response
                    (JSON->TEXT)

Boot-time cost: VARCHAR->TEXT is a metadata-only change (no rewrite). JSON->TEXT
rewrites those columns; on a large ``results`` table this takes a short lock
under the migration advisory lock — acceptable one-time cost, no data churn.

Downgrade is only safe PRE-backfill (while values are still plaintext / valid
JSON). After encryption, ciphertext cannot cast back to JSON and would overflow
the old VARCHAR widths — do not downgrade a backfilled database.

Revision ID: 046
Revises: 045
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # results — scalar contact PII (VARCHAR -> TEXT, metadata-only)
    op.alter_column("results", "phone", type_=sa.Text(), existing_nullable=True)
    op.alter_column("results", "email", type_=sa.Text(), existing_nullable=True)
    # results — multi-contact JSON -> TEXT (holds ciphertext string)
    op.alter_column(
        "results", "phones", type_=sa.Text(),
        postgresql_using="phones::text", existing_nullable=True,
    )
    op.alter_column(
        "results", "emails", type_=sa.Text(),
        postgresql_using="emails::text", existing_nullable=True,
    )

    # skip_trace_cache — scalar contact PII
    op.alter_column("skip_trace_cache", "phone", type_=sa.Text(), existing_nullable=True)
    op.alter_column("skip_trace_cache", "email", type_=sa.Text(), existing_nullable=True)
    # skip_trace_cache — JSON -> TEXT (multi-contact + full provider payload)
    op.alter_column(
        "skip_trace_cache", "phones", type_=sa.Text(),
        postgresql_using="phones::text", existing_nullable=True,
    )
    op.alter_column(
        "skip_trace_cache", "emails", type_=sa.Text(),
        postgresql_using="emails::text", existing_nullable=True,
    )
    op.alter_column(
        "skip_trace_cache", "raw_response", type_=sa.Text(),
        postgresql_using="raw_response::text", existing_nullable=True,
    )


def downgrade() -> None:
    # NOTE: only safe pre-backfill. Casts assume plaintext / valid-JSON values.
    op.alter_column(
        "skip_trace_cache", "raw_response", type_=sa.JSON(),
        postgresql_using="raw_response::json", existing_nullable=True,
    )
    op.alter_column(
        "skip_trace_cache", "emails", type_=sa.JSON(),
        postgresql_using="emails::json", existing_nullable=True,
    )
    op.alter_column(
        "skip_trace_cache", "phones", type_=sa.JSON(),
        postgresql_using="phones::json", existing_nullable=True,
    )
    op.alter_column("skip_trace_cache", "email", type_=sa.String(length=255), existing_nullable=True)
    op.alter_column("skip_trace_cache", "phone", type_=sa.String(length=32), existing_nullable=True)

    op.alter_column(
        "results", "emails", type_=sa.JSON(),
        postgresql_using="emails::json", existing_nullable=True,
    )
    op.alter_column(
        "results", "phones", type_=sa.JSON(),
        postgresql_using="phones::json", existing_nullable=True,
    )
    op.alter_column("results", "email", type_=sa.String(length=255), existing_nullable=True)
    op.alter_column("results", "phone", type_=sa.String(length=32), existing_nullable=True)
