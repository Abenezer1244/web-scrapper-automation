"""H3 Phase 3: encrypt existing contact-PII rows at rest.

Run MANUALLY after migration 046 is applied (widens the columns to TEXT) — never
on API boot (the results table can be large; an in-migration backfill would brick
the advisory-locked deploy).

Encrypts the in-scope private contact PII in place, leaving names/addresses
(public-record, ILIKE-searched) untouched:
  results:          phone, email, phones, emails
  skip_trace_cache: phone, email, phones, emails, raw_response

Why Python, not SQL: Fernet is non-deterministic, so the ciphertext is computed
per value in Python. We read the RAW stored value via text() (bypassing the
EncryptedString/EncryptedJSON type), compute the target, and write the ciphertext
back via text() (which stores the bound string verbatim).

Idempotent + re-runnable:
  * a value already encrypted (fe1: + decrypt-validated, or a legacy bare Fernet
    token) is kept as-is — never double-encrypted;
  * blank/whitespace-only values normalize to NULL (so contactability predicates
    like `trim(phone) != ''` / `IS NOT NULL` stay correct — no empty ciphertext);
  * NULL stays NULL.
A SQL pre-filter (`col NOT LIKE 'fe1:%'`) skips already-done rows cheaply on
re-runs; the Python is_encrypted() check is the authoritative guard. None of the
in-scope PII plaintext (digits / emails / JSON arrays) starts with 'fe1:', so the
pre-filter never skips an un-encrypted legacy value.

Keyset pagination (no OFFSET); small batched commits.

Usage:  railway run --service worker python scripts/backfill_pii_encryption.py [--batch 2000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.utils.crypto import encrypt_field, is_encrypted

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_pii_encryption")


def _target(value: str | None) -> str | None:
    """The value a column should hold after backfill."""
    if value is None:
        return None
    if is_encrypted(value):
        return value  # already ciphertext (fe1: or legacy bare) — keep
    if value.strip() == "":
        return None  # blank -> NULL (no empty-string ciphertext)
    return encrypt_field(value)


def _backfill_table(
    table: str, pk: str, pk_cast: str, columns: list[str], batch: int
) -> None:
    """Encrypt `columns` of `table`, keyset-paginated by `pk`."""
    # Seed cursor below any real key. results.id is a uuid; skip_trace_cache
    # address_hash is text — empty string sorts first.
    last = "00000000-0000-0000-0000-000000000000" if pk_cast == "uuid" else ""
    needs_work = " OR ".join(
        f"({c} IS NOT NULL AND {c} NOT LIKE 'fe1:%')" for c in columns
    )
    col_list = ", ".join(columns)
    set_clause = ", ".join(f"{c} = :{c}" for c in columns)
    # Identifiers (table/pk/columns) are internal constants from run(); all
    # row values are bound params — no user input is interpolated.
    select_sql = (
        f"SELECT {pk}, {col_list} FROM {table} "  # noqa: S608
        f"WHERE {pk} > CAST(:last AS {pk_cast}) AND ({needs_work}) "
        f"ORDER BY {pk} LIMIT :batch"
    )
    update_sql = (
        f"UPDATE {table} SET {set_clause} WHERE {pk} = CAST(:pk AS {pk_cast})"  # noqa: S608
    )
    scanned = 0
    changed = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(select_sql), {"last": last, "batch": batch}
            ).fetchall()
            if not rows:
                break
            last = str(getattr(rows[-1], pk))

            updates: list[dict] = []
            for row in rows:
                scanned += 1
                m = row._mapping
                new_vals = {c: _target(m[c]) for c in columns}
                # Only write if something actually changes (a row may be selected
                # because one column needs work while others are already done).
                if any(new_vals[c] != m[c] for c in columns):
                    updates.append({"pk": str(m[pk]), **new_vals})
            if updates:
                db.execute(text(update_sql), updates)
                changed += len(updates)
            db.commit()
            _log.info("%s: scanned %d, changed %d — through %s", table, scanned, changed, last)
    _log.info("%s: DONE — scanned %d, changed %d", table, scanned, changed)


def run(batch: int) -> None:
    _backfill_table(
        "results", "id", "uuid", ["phone", "email", "phones", "emails"], batch
    )
    _backfill_table(
        "skip_trace_cache",
        "address_hash",
        "text",
        ["phone", "email", "phones", "emails", "raw_response"],
        batch,
    )
    _log.info("H3 PII backfill complete.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000)
    run(ap.parse_args().batch)
