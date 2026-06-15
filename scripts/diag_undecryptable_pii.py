"""READ-ONLY diagnostic for the 2026-06-15 InvalidToken incident.

`run_scrape_job` raised `InvalidToken('fe1:-prefixed value is not decryptable
under strict mode')`: an 'fe1:' field-encryption token that no key in the current
FIELD_ENCRYPTION_KEY set can decrypt. The derived key was dropped after the
2026-06-13 incident, so reencrypt_derived_key_pii.py can't be re-run as-is (it
REQUIRES a 2-key env and aborts on primary-only). This scan works with the current
(primary-only) env AND additionally tests the HKDF-from-SECRET_KEY derived key, to
tell a RECOVERABLE derived-key recurrence from an unrecoverable anomaly.

Classification per non-null 'fe1:' token:
  current      -> decrypts under the live FIELD_ENCRYPTION_KEY set (healthy)
  derived_hkdf -> fails current, decrypts under HKDF(SECRET_KEY) (RECOVERABLE: the
                  no-key-window fallback recurred; re-add derived key + re-encrypt)
  anomaly      -> decrypts under NEITHER (unknown/lost key — needs investigation)
Unprefixed values are skipped (legacy bare tokens / plaintext are a different path;
this incident is specifically about fe1:-prefixed tokens).

NEVER prints plaintext. Counts + sample PKs only.

  railway run --service worker python scripts/diag_undecryptable_pii.py            # READ-ONLY scan
  railway run --service worker python scripts/diag_undecryptable_pii.py --apply     # re-encrypt derived_hkdf onto primary

--apply recovers `derived_hkdf` tokens: decrypt with the HKDF key, re-encrypt with the PRIMARY key
(`encrypt_field`), re-verify the new token under the current set BEFORE writing, then UPDATE. It NEVER
touches `anomaly` or `current` rows and is idempotent (a second run finds them all `current` and skips).
No env mutation — the derived key is computed via HKDF, never added to FIELD_ENCRYPTION_KEY.
"""
import argparse
import os
import sys

sys.path.insert(0, ".")

from cryptography.fernet import Fernet, InvalidToken, MultiFernet  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from src.utils.crypto import _ENC_PREFIX, _derive_key_from_secret, encrypt_field  # noqa: E402

_BATCH = 200


def reencrypt_derived_to_primary(raw_value: str, *, derived: Fernet, current: MultiFernet) -> str | None:
    """Return a PRIMARY-key fe1: token for a derived-key token, or None to skip.

    None when the value is not an fe1: token, is already current-decryptable, or is
    an anomaly (derived.decrypt raises -> propagates; caller only passes derived rows).
    Re-verifies the new token under `current` before returning so a write can never
    store garbage. Pure/testable: no DB, no env mutation.
    """
    if not raw_value.startswith(_ENC_PREFIX):
        return None
    tok = raw_value[len(_ENC_PREFIX):].encode("ascii")
    try:
        current.decrypt(tok)
        return None  # already on a current key — nothing to do
    except InvalidToken:
        pass
    plaintext = derived.decrypt(tok).decode("utf-8")  # raises if not derived (caller filters)
    new_val = encrypt_field(plaintext)                # encrypts under the PRIMARY (env) key
    current.decrypt(new_val[len(_ENC_PREFIX):].encode("ascii"))  # re-verify before write
    return new_val

# (table, pk_column, value_column) — the 11 EncryptedString/EncryptedJSON columns.
_COLUMNS = [
    ("users", "id", "email"),
    ("results", "id", "phone"),
    ("results", "id", "email"),
    ("results", "id", "phones"),
    ("results", "id", "emails"),
    ("skip_trace_cache", "address_hash", "phone"),
    ("skip_trace_cache", "address_hash", "email"),
    ("skip_trace_cache", "address_hash", "phones"),
    ("skip_trace_cache", "address_hash", "emails"),
    ("skip_trace_cache", "address_hash", "raw_response"),
    ("skip_trace_queues", "id", "download_url"),
]


def _engine():
    dsn = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL_SYNC")
    if not dsn:
        print("ABORT: set DATABASE_URL_MIGRATE (privileged BYPASSRLS DSN) or DATABASE_URL_SYNC")
        sys.exit(1)
    return create_engine(dsn, future=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="re-encrypt derived_hkdf tokens onto the primary key (default: read-only scan)")
    args = ap.parse_args()

    raw = (os.environ.get("FIELD_ENCRYPTION_KEY") or "").strip()
    env_keys = [k.strip() for k in raw.split(",") if k.strip()]
    current = MultiFernet([Fernet(k) for k in env_keys]) if env_keys else None
    derived_key = _derive_key_from_secret()
    derived = Fernet(derived_key)
    derived_in_env = derived_key.decode("ascii") in env_keys
    if args.apply and current is None:
        print("ABORT: --apply needs a non-empty FIELD_ENCRYPTION_KEY (primary) to re-encrypt onto.")
        sys.exit(1)

    def classify(val: str) -> str | None:
        if not val.startswith(_ENC_PREFIX):
            return None  # unprefixed — not this incident's failure mode
        tok = val[len(_ENC_PREFIX):].encode("ascii")
        if current is not None:
            try:
                current.decrypt(tok)
                return "current"
            except InvalidToken:
                pass
        try:
            derived.decrypt(tok)
            return "derived_hkdf"
        except InvalidToken:
            return "anomaly"

    grand = {"current": 0, "derived_hkdf": 0, "anomaly": 0, "unprefixed_skipped": 0,
             "reencrypted": 0, "cas_skipped": 0}
    engine = _engine()
    with engine.connect() as conn:
        who = conn.execute(text("SELECT current_user")).scalar()
        print(
            f"connected_as={who} env_keys={len(env_keys)} "
            f"derived_key_in_env={derived_in_env} mode={'APPLY' if args.apply else 'READ-ONLY'}"
        )
        if derived_in_env:
            print("NOTE: the HKDF-derived key IS one of the live env keys — "
                  "'derived_hkdf' cannot occur separately from 'current'.")
        for table, pk, col in _COLUMNS:
            rows = conn.execute(text(
                f"SELECT {pk} AS pk, {col} AS val FROM {table} "  # noqa: S608 — fixed allowlist
                f"WHERE {col} IS NOT NULL AND {col} <> ''"
            )).fetchall()
            c = {"current": 0, "derived_hkdf": 0, "anomaly": 0, "unprefixed_skipped": 0,
                 "reencrypted": 0, "cas_skipped": 0}
            bad: list = []
            for r in rows:
                kind = classify(r.val)
                if kind is None:
                    c["unprefixed_skipped"] += 1
                    continue
                c[kind] += 1
                if kind in ("derived_hkdf", "anomaly"):
                    bad.append((r.pk, kind))
                if kind == "derived_hkdf" and args.apply:
                    new_val = reencrypt_derived_to_primary(r.val, derived=derived, current=current)
                    if new_val is not None:
                        # Compare-and-swap on the OLD ciphertext: if the row changed
                        # between SELECT and now (e.g. the user updated their email),
                        # rowcount=0 and we skip rather than revert their new value.
                        res = conn.execute(
                            text(f"UPDATE {table} SET {col} = :v "  # noqa: S608 — fixed allowlist
                                 f"WHERE {pk} = :pk AND {col} = :old"),
                            {"v": new_val, "pk": r.pk, "old": r.val},
                        )
                        if res.rowcount == 1:
                            c["reencrypted"] += 1
                            if c["reencrypted"] % _BATCH == 0:
                                conn.commit()
                        else:
                            c["cas_skipped"] += 1
                            print(f"     CAS skip (row changed concurrently): {table}.{col} pk={r.pk}")
            if args.apply:
                conn.commit()
            for k in grand:
                grand[k] += c[k]
            flag = "  <== BAD" if (c["derived_hkdf"] or c["anomaly"]) else ""
            print(f"  {table}.{col}: current={c['current']} derived_hkdf={c['derived_hkdf']} "
                  f"anomaly={c['anomaly']} unprefixed={c['unprefixed_skipped']} "
                  f"reencrypted={c['reencrypted']} cas_skipped={c['cas_skipped']}{flag}")
            if bad:
                print(f"     bad pks: {bad[:10]}{' …' if len(bad) > 10 else ''}")

    print(f"TOTAL: {grand}")
    if grand["derived_hkdf"] == 0 and grand["anomaly"] == 0:
        print("CLEAN: every fe1: token decrypts under the current key set.")
    elif grand["anomaly"] == 0:
        if args.apply:
            print(f"APPLIED: re-encrypted {grand['reencrypted']} derived-key token(s) onto the primary key. "
                  "Re-run WITHOUT --apply to confirm derived_hkdf=0.")
        else:
            print("RECOVERABLE: derived-key (HKDF) tokens present — re-run this script with --apply to "
                  "re-encrypt them onto the primary key (self-contained, no env change), then re-scan.")
    else:
        print("ANOMALY PRESENT: tokens decrypt under NEITHER the current set NOR the HKDF key "
              "— encrypted by an unknown/lost key. Needs deeper investigation before any fix.")


if __name__ == "__main__":
    main()
