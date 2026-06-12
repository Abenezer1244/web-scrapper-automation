"""H3 Phase 5: encrypt users.email at rest (run AFTER the P5 deploy).

Order matters. P5 deploys the code that (a) reads users by email_hmac instead of
the email value and (b) encrypts email on write. ONLY THEN is it safe to encrypt
the existing plaintext emails — before the read switch, encrypting email would
break login (lookups still compared the plaintext value).

Encrypts users.email in place. Does NOT touch email_hmac: the blind index is
HMAC(normalized PLAINTEXT email) and must stay that — encrypting the display
value does not change it, and the P4 reconcile already set it under the final key.

Idempotent + re-runnable: skips rows already fe1:-encrypted (is_encrypted guard;
SQL pre-filter `NOT LIKE 'fe1:%'` makes re-runs cheap). Keyset pagination.

Usage:  railway run --service worker python scripts/backfill_user_email_encrypt.py [--batch 1000]
"""
import argparse
import logging
import sys

sys.path.insert(0, ".")  # railway-run: scripts/ is sys.path[0], repo root is not

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.utils.crypto import encrypt_field, is_encrypted

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_user_email_encrypt")


def run(batch: int) -> None:
    last = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    changed = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(
                    """
                    SELECT id, email FROM users
                    WHERE id > CAST(:last AS uuid)
                      AND email IS NOT NULL AND email NOT LIKE 'fe1:%'
                    ORDER BY id
                    LIMIT :batch
                    """
                ),
                {"last": last, "batch": batch},
            ).fetchall()
            if not rows:
                break
            last = str(rows[-1].id)
            scanned += len(rows)
            updates = [
                {"pk": str(r.id), "email": encrypt_field(r.email)}
                for r in rows
                if r.email and not is_encrypted(r.email)
            ]
            if updates:
                db.execute(
                    text("UPDATE users SET email = :email WHERE id = CAST(:pk AS uuid)"),
                    updates,
                )
                changed += len(updates)
            db.commit()
            _log.info("users.email: scanned %d, changed %d — through id %s", scanned, changed, last)
    _log.info("users.email: DONE — scanned %d, encrypted %d", scanned, changed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1000)
    run(ap.parse_args().batch)
