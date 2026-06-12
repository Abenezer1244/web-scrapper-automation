"""Encrypt SkipTraceQueue.download_url at rest (H3 residual). (054)

The Tracerfy webhook stores a signed download link whose target CSV contains
traced PII (phones/emails). H3 deferred this column; with the program complete
(strict mode live), it joins the encrypted scope.

ONE-deploy rollout under PII_ENCRYPTION_STRICT=true (Codex-reconciled):
  1. Data minimization first: NULL download_url on rows completed > 7 days ago —
     the signed links are long-expired; an expired bearer URL is pure liability.
  2. Encrypt the remaining non-NULL values IN-MIGRATION via RAW SQL — the ORM
     must not be used here: the model flips to EncryptedString in this same
     deploy, and its result processor would RAISE on the still-plaintext values
     under strict mode before we could encrypt them (Codex P1).
  3. Fail closed: assert every remaining non-NULL value is_encrypted, else
     abort the migration (and therefore the deploy).

Idempotent: is_encrypted() guards re-runs. The column is already Text (no
widen). Old-replica write window during the rolling deploy is closed
operationally: writes only happen on Tracerfy completion webhooks, and the
account is out of credits (0 in-flight completions) — verified pre-merge.

Revision ID: 054
Revises: 053
Create Date: 2026-06-12
"""
from alembic import op
from sqlalchemy import text as sa_text

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Lazy imports (matches 053): alembic env must not need app settings at
    # module import time; crypto pulls Settings only when actually run.
    from src.utils.crypto import encrypt_field, is_encrypted

    conn = op.get_bind()

    # 1. Expired-link minimization (links are 48h-ish; 7 days is generous).
    conn.execute(
        sa_text(
            "UPDATE skip_trace_queues SET download_url = NULL "
            "WHERE download_url IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND completed_at < now() - interval '7 days'"
        )
    )

    # 2. Encrypt what remains (raw SQL — never the ORM here; see module doc).
    rows = conn.execute(
        sa_text(
            "SELECT id, download_url FROM skip_trace_queues "
            "WHERE download_url IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        if is_encrypted(row.download_url):
            continue  # idempotent re-run
        conn.execute(
            sa_text("UPDATE skip_trace_queues SET download_url = :v WHERE id = :id"),
            {"v": encrypt_field(row.download_url), "id": row.id},
        )

    # 3. Fail closed: any plaintext remaining means strict-mode reads would
    #    raise after this deploy — refuse to complete the migration instead.
    remaining = conn.execute(
        sa_text(
            "SELECT id, download_url FROM skip_trace_queues "
            "WHERE download_url IS NOT NULL"
        )
    ).fetchall()
    bad = [r.id for r in remaining if not is_encrypted(r.download_url)]
    if bad:
        raise RuntimeError(
            f"054 refused: {len(bad)} skip_trace_queues.download_url value(s) "
            f"still plaintext after encryption pass (ids: {bad[:5]}…). "
            "Strict-mode reads would raise — investigate before deploying."
        )


def downgrade() -> None:
    # Ciphertext stays readable through the app's tolerant decrypt path; no
    # schema change to revert (column was already Text). Decrypting at rest on
    # downgrade would be a PII regression — intentionally not done.
    pass
