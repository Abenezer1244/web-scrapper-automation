"""H3 Phase 4: populate users.email_hmac from the (plaintext) email.

Run MANUALLY after the P4 migration adds users.email_hmac (nullable) — never on
boot. This is the PREREQUISITE for the P5 cutover: the UNIQUE constraint on
email_hmac and the login lookup switch are only safe once EVERY existing user
row has its blind index populated (otherwise rows with NULL email_hmac become
unloggable). Verify this script reports 0 remaining NULLs before deploying P5.

Idempotent + re-runnable: only rows with email_hmac IS NULL are touched. The
blind index is computed with crypto.blind_index (the SAME function the @validates
choke point + the login lookup use), so backfilled and forward rows match.

Keyset pagination by users.id; small batched commits.

Usage:  railway run --service worker python scripts/backfill_user_email_hmac.py [--batch 1000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.utils.crypto import blind_index

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_user_email_hmac")


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
                    WHERE id > CAST(:last AS uuid) AND email_hmac IS NULL
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
                {"pk": str(r.id), "hmac": blind_index(r.email)}
                for r in rows
                if r.email
            ]
            if updates:
                db.execute(
                    text("UPDATE users SET email_hmac = :hmac WHERE id = CAST(:pk AS uuid)"),
                    updates,
                )
                changed += len(updates)
            db.commit()
            _log.info("users: scanned %d, changed %d — through id %s", scanned, changed, last)
    remaining = SyncSessionLocal().execute(
        text("SELECT count(*) FROM users WHERE email_hmac IS NULL")
    ).scalar()
    _log.info(
        "DONE — scanned %d, changed %d, remaining NULL email_hmac = %d "
        "(must be 0 before deploying P5)",
        scanned, changed, remaining,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=1000)
    run(ap.parse_args().batch)
