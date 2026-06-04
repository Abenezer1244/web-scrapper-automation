"""Best-effort historical backfill for results.property_key (Phase 3).

Run MANUALLY after migration 037 is applied — never on API boot (the results
table can be large; an in-migration backfill would brick the advisory-locked
deploy). Re-runnable and idempotent: only NULL property_key rows are updated, so
re-running never recomputes or clobbers an existing value.

property_key is computed straight from results.parcel_id/property_address via
the SAME property_identity.compute_property_key the live worker path uses — no
join to jobs/scraper_configs is needed (unlike the membership backfill), since
identity is a function of the property alone, independent of record_type.

ALL computable rows are backfilled, including is_duplicate=true: property_key is
property identity, not result visibility (Codex review). Only weak-identity rows
(compute_property_key -> None) stay NULL.

Keyset pagination by results.id (no OFFSET); small batched commits.

Usage:  python scripts/backfill_result_property_key.py [--batch 5000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.workers.property_identity import compute_property_key

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("backfill_result_property_key")


def run(batch: int) -> None:
    # results.id is a UUID column. Seed the keyset cursor with the nil UUID
    # (the minimum possible value) — NOT "" — because Postgres casts the bound
    # param to uuid for `id > :last_id`, and an empty string raises
    # "invalid input syntax for type uuid" before any row is scanned (Codex
    # review). CAST is explicit so the comparison is unambiguously uuid > uuid.
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    updated = 0
    weak = 0
    while True:
        with SyncSessionLocal() as db:
            # Only rows still missing a key. Keyset by id so we never re-scan and
            # never use OFFSET. Select just what we need to compute the key.
            rows = db.execute(
                text(
                    """
                    SELECT id, parcel_id, property_address
                    FROM results
                    WHERE id > CAST(:last_id AS uuid) AND property_key IS NULL
                    ORDER BY id
                    LIMIT :batch
                    """
                ),
                {"last_id": last_id, "batch": batch},
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            for row in rows:
                scanned += 1
                key = compute_property_key(row.parcel_id, row.property_address)
                if not key:
                    weak += 1
                    continue
                db.execute(
                    text(
                        """
                        UPDATE results
                        SET property_key = :pk
                        WHERE id = :id AND property_key IS NULL
                        """
                    ),
                    {"pk": key, "id": str(row.id)},
                )
                updated += 1
            db.commit()
            _log.info(
                "scanned %d (updated %d, weak %d) — through id %s",
                scanned, updated, weak, last_id,
            )
    _log.info("done — scanned %d, updated %d, weak-identity skipped %d", scanned, updated, weak)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    run(ap.parse_args().batch)
