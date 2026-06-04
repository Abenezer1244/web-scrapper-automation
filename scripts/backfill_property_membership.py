"""Best-effort historical backfill for property_list_membership (Phase 1).

Run MANUALLY after migration 034 is applied — never on API boot. Idempotent
(re-runnable), batched, small commits. Best-effort by design: record_type was
never snapshotted on results/jobs, so it joins results -> jobs -> scraper_configs
and uses the config's CURRENT record_type. Properties whose config changed type
or was deleted are approximate or skipped. Forward accrual (workers/tasks.py) is
the source of truth; this only seeds pre-launch history.

Usage:  python scripts/backfill_property_membership.py [--batch 5000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.workers.property_identity import compute_property_key

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("backfill_membership")


def run(batch: int) -> None:
    last_id = ""
    total = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(
                    """
                    SELECT r.id, r.user_id, r.parcel_id, r.property_address,
                           sc.record_type
                    FROM results r
                    JOIN jobs j ON j.id = r.job_id
                    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                    WHERE r.id > :last_id
                    ORDER BY r.id
                    LIMIT :batch
                    """
                ),
                {"last_id": last_id, "batch": batch},
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1].id
            agg: dict[tuple, dict] = {}
            for row in rows:
                key = compute_property_key(row.parcel_id, row.property_address)
                if not key or not row.record_type:
                    continue
                k = (str(row.user_id), row.record_type, key)
                cur = agg.get(k)
                if cur is None:
                    agg[k] = {"parcel_id": row.parcel_id, "property_address": row.property_address, "count": 1}
                else:
                    cur["count"] += 1
            for (uid, rt, key), v in sorted(agg.items()):
                db.execute(
                    text(
                        """
                        INSERT INTO property_list_membership
                            (user_id, record_type, property_key, parcel_id,
                             property_address, sighting_count, first_seen_at, last_seen_at)
                        VALUES (:uid, :rt, :pk, :pid, :addr, :cnt, NOW(), NOW())
                        ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                            sighting_count = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                            parcel_id = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                            property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
                        """
                    ),
                    {"uid": uid, "rt": rt, "pk": key,
                     "pid": v["parcel_id"], "addr": v["property_address"], "cnt": v["count"]},
                )
            db.commit()
            total += len(rows)
            _log.info("backfilled through result id %s (%d rows scanned)", last_id, total)
    _log.info("done — %d result rows scanned", total)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    run(ap.parse_args().batch)
