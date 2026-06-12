"""H3 Phase 5: User.email encryption cutover. (048)

Moves the searchable key + uniqueness from the (now-encrypted) email column to the
email_hmac blind index, and widens email to TEXT for ciphertext.

PRECONDITION — DEPLOY STAGE 2 ONLY (must hold, or it breaks):
  * P4 (migration 047 + the @validates email_hmac dual-write) has ALREADY fully
    rolled out. Railway does rolling deploys, so the NOT NULL below is only safe
    once the currently-serving code already populates email_hmac on insert —
    otherwise an old replica's /auth/register during the overlap hits NOT NULL.
    Do NOT ship 047 and 048 in the same deploy.
  * scripts/backfill_user_email_hmac.py has run under the FINAL BLIND_INDEX_KEY
    and reports 0 NULL + 0 changed (every row has the correct blind index).
If email_hmac has any NULL, the NOT NULL alter fails and the deploy will not boot
— a deliberate fail-closed guard, not a silent half-cutover. Because email
historically had a UNIQUE constraint, no duplicate emails exist, so the
deterministic email_hmac is collision-free and the UNIQUE add is safe.

users.email values may still be plaintext at this point; the new code reads by
email_hmac and decrypts email tolerantly, so existing rows work. Encrypt them
afterwards with scripts/backfill_user_email_encrypt.py, then run
scripts/verify_pii_encryption.py and flip PII_ENCRYPTION_STRICT.

Downgrade is only safe pre-email-encryption (ciphertext overflows VARCHAR(255)).

Revision ID: 053
Revises: 052
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text as sa_text

# src.config.settings + src.utils.crypto are imported lazily inside upgrade() (not
# at module top level) so that loading this script to build the Alembic revision
# graph does not instantiate Settings — keeps migration-only environments working.

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.config import settings
    from src.utils.crypto import blind_index, decrypt_field

    # email -> ciphertext at rest: drop its index + unique constraint, widen to TEXT.
    # Defensive IF EXISTS: this runs on API boot, so a slightly different
    # auto-generated constraint/index name must not hard-fail the deploy. The
    # names below are the Postgres/SQLAlchemy defaults from 001_initial
    # (column unique=True -> users_email_key; create_index -> ix_users_email).
    op.execute("DROP INDEX IF EXISTS ix_users_email")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key")
    op.alter_column("users", "email", type_=sa.Text(), existing_nullable=False)

    # email_hmac becomes the searchable key: NOT NULL + UNIQUE. Reconcile it
    # IN-MIGRATION first (Codex P1): alembic runs 047 then 048 in one boot, so a
    # bare NOT NULL would block the deploy on existing rows that 047 added as
    # NULL. The users table is small, so reconciling here (recompute every row's
    # blind index under the CURRENT key — correcting any fallback-key value) is
    # cheap and makes a single combined deploy safe. email is still plaintext at
    # this point, so blind_index reads the real address. Provision the dedicated
    # BLIND_INDEX_KEY before this deploys so the hashes match the login lookup.
    conn = op.get_bind()
    rows = conn.execute(sa_text("SELECT id, email, email_hmac FROM users")).fetchall()
    # Fail closed (Codex P2): without the dedicated BLIND_INDEX_KEY this would hash
    # every email under the SECRET_KEY fallback; provisioning the real key (or
    # rotating SECRET_KEY) later changes blind_index(email) and locks those users
    # out. Refuse the cutover whenever it is unsafe:
    #   * any existing users (their hashes would be fallback-derived), OR
    #   * a production environment even on an EMPTY DB — a fresh prod/staging would
    #     otherwise register users under the fallback before the key is provisioned
    #     (Codex P2: an empty row count must not bypass the guard in prod).
    # Non-production (CI sets ENVIRONMENT=test) on an empty DB is harmless, so the
    # HKDF-from-SECRET_KEY dev fallback stays allowed there.
    key_set = bool((settings.BLIND_INDEX_KEY or "").strip())
    is_production = (settings.ENVIRONMENT or "").strip().lower() == "production"
    if not key_set and (rows or is_production):
        raise RuntimeError(
            "H3 cutover refused: BLIND_INDEX_KEY is not set"
            f"{' but users exist' if rows else ' in a production environment'}. Set a "
            "dedicated BLIND_INDEX_KEY in the environment (never the SECRET_KEY "
            "fallback) before running migration 048, or logins will break when the "
            "key is later provisioned."
        )
    for row in rows:
        if not row.email:
            continue  # NOT NULL below will (correctly) reject a user with no email
        # decrypt_field is tolerant: plaintext passes through, fe1: ciphertext
        # decrypts. So the blind index is always over the PLAINTEXT email even if
        # this migration re-runs after email was encrypted (Codex P1) — never hash
        # the ciphertext, which would break login lookups.
        want = blind_index(decrypt_field(row.email))
        if row.email_hmac != want:
            conn.execute(
                sa_text("UPDATE users SET email_hmac = :h WHERE id = :id"),
                {"h": want, "id": row.id},
            )

    op.alter_column(
        "users", "email_hmac", existing_type=sa.String(length=64), nullable=False
    )
    op.drop_index("ix_users_email_hmac", table_name="users")
    # UNIQUE add fails (correctly) if case-variant duplicate emails collided on the
    # normalized blind index — run scripts/backfill_user_email_hmac.py first to
    # detect that gracefully.
    op.create_unique_constraint("users_email_hmac_key", "users", ["email_hmac"])


def downgrade() -> None:
    # Only safe before users.email has been encrypted.
    op.drop_constraint("users_email_hmac_key", "users", type_="unique")
    op.create_index("ix_users_email_hmac", "users", ["email_hmac"])
    op.alter_column(
        "users", "email_hmac", existing_type=sa.String(length=64), nullable=True
    )
    op.alter_column("users", "email", type_=sa.String(length=255), existing_nullable=False)
    op.create_unique_constraint("users_email_key", "users", ["email"])
    op.create_index("ix_users_email", "users", ["email"])
