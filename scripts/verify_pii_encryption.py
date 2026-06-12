"""H3 Phase 5: verify all in-scope PII is encrypted before flipping strict mode.

Run after every backfill (contact PII + user email_hmac + user email). Gate for
setting PII_ENCRYPTION_STRICT=true: exits 0 only when every in-scope value is
properly encrypted AND every user has an email_hmac.

DECRYPT-VALIDATED (Codex P2): a value is counted clean only if it actually
decrypts under the CURRENT key — not merely if it starts with `fe1:`. A token
encrypted under a missing/old FIELD_ENCRYPTION_KEY, or a corrupt token, fails
is_encrypted() here and is flagged, so strict mode (which would raise
InvalidToken on read) is never green-lit on undecryptable ciphertext.

Read-only. Scans every in-scope value, so on a large `results` table it does a
full table read + a Fernet decrypt per non-null value — minutes, one-time.

Usage:  railway run --service worker python scripts/verify_pii_encryption.py [--batch 2000]
"""
import argparse
import logging
import sys

sys.path.insert(0, ".")  # railway-run: scripts/ is sys.path[0], repo root is not

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.utils.crypto import is_encrypted

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("verify_pii_encryption")

# (table, pk, pk_cast, encrypted_columns)
_TABLES = [
    ("results", "id", "uuid", ["phone", "email", "phones", "emails"]),
    ("skip_trace_cache", "address_hash", "text",
     ["phone", "email", "phones", "emails", "raw_response"]),
    ("users", "id", "uuid", ["email"]),
]


def _scan_table(table, pk, pk_cast, columns, batch):
    last = "00000000-0000-0000-0000-000000000000" if pk_cast == "uuid" else ""
    col_list = ", ".join(columns)
    bad = dict.fromkeys(columns, 0)
    select_sql = (
        f"SELECT {pk}, {col_list} FROM {table} "  # noqa: S608 — internal identifiers
        f"WHERE {pk} > CAST(:last AS {pk_cast}) ORDER BY {pk} LIMIT :batch"
    )
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(text(select_sql), {"last": last, "batch": batch}).fetchall()
        if not rows:
            break
        last = str(rows[-1]._mapping[pk])
        for r in rows:
            m = r._mapping
            for c in columns:
                v = m[c]
                # Flag plaintext AND fe1: ciphertext that won't decrypt now.
                if v is not None and not is_encrypted(v):
                    bad[c] += 1
    return bad


def run(batch: int) -> int:
    problems = 0
    _log.info("%-22s %-14s %s", "TABLE", "COLUMN", "NOT_ENCRYPTED_OR_UNDECRYPTABLE")
    for table, pk, pk_cast, columns in _TABLES:
        bad = _scan_table(table, pk, pk_cast, columns, batch)
        for c in columns:
            flag = "" if bad[c] == 0 else "  <-- FIX"
            _log.info("%-22s %-14s %d%s", table, c, bad[c], flag)
            problems += bad[c]

    with SyncSessionLocal() as db:
        null_hmac = db.execute(
            text("SELECT count(*) FROM users WHERE email_hmac IS NULL")
        ).scalar()
    flag = "" if null_hmac == 0 else "  <-- MISSING BLIND INDEX"
    _log.info("%-22s %-14s %d%s", "users", "email_hmac(NULL)", null_hmac, flag)
    problems += null_hmac

    if problems == 0:
        _log.info("\nALL CLEAR — safe to set PII_ENCRYPTION_STRICT=true.")
        return 0
    _log.info("\n%d value(s) still need backfill / re-key — do NOT flip strict mode yet.", problems)
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000)
    sys.exit(run(ap.parse_args().batch))
