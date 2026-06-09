"""H3 Phase 4: reconcile users.email_hmac to blind_index(email) under the CURRENT key.

Run MANUALLY after the P4 migration adds users.email_hmac (nullable) — never on
boot. This is the PREREQUISITE for the P5 cutover: the UNIQUE constraint on
email_hmac and the login-lookup switch are only safe once EVERY user row's blind
index matches blind_index(email) under the key that P5 will use to look users up.

RECONCILING, not just fill-NULL (Codex P1): if P4 is deployed before the
dedicated BLIND_INDEX_KEY is provisioned, the @validates dual-write computes
email_hmac under the SECRET_KEY fallback. A fill-NULL-only backfill would skip
those non-null rows, and once BLIND_INDEX_KEY is set their hashes would no longer
match blind_index(email) — locking those users out after the P5 read switch. So
this script recomputes EVERY row under the current key and rewrites any that
differ. email_hmac is unused for lookups until P5, so correcting it here is safe.

=> RUN THIS AFTER BLIND_INDEX_KEY IS FINALIZED IN PROD, and immediately before
   deploying P5. Re-run until it reports 0 changed + 0 NULL.

Idempotent: same key -> same hash -> no rewrite. Keyset pagination by users.id.

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
            # Scan ALL rows (not just NULL email_hmac) so a hash computed under a
            # stale/fallback key gets corrected to the current key (Codex P1).
            rows = db.execute(
                text(
                    """
                    SELECT id, email, email_hmac FROM users
                    WHERE id > CAST(:last AS uuid)
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
            # Only rewrite rows whose stored hash differs from the current key's.
            updates = []
            for r in rows:
                if not r.email:
                    continue
                want = blind_index(r.email)
                if r.email_hmac != want:
                    updates.append({"pk": str(r.id), "hmac": want})
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
