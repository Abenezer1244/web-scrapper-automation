"""Best-effort historical backfill for property_list_membership (Phase 1).

Run MANUALLY after migration 034 is applied — never on API boot. Re-runnable
(safe: PK + ON CONFLICT never create duplicate rows), batched, small commits.
NOTE: re-running re-adds to the advisory `sighting_count` (NOT idempotent — by
design, since sighting_count is advisory and never used for billing or overlap
correctness). Best-effort by design: record_type was
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
# Silence SQLAlchemy per-statement echo — bulk backfill runs many statements.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_membership")


def run(batch: int) -> None:
    # results.id is UUID: seed the keyset cursor with the nil UUID (min value),
    # NOT "" — an empty string raises "invalid input syntax for type uuid" on
    # the first `r.id > :last_id` query before any row is scanned (Codex review,
    # found via the Phase 3 twin backfill).
    last_id = "00000000-0000-0000-0000-000000000000"
    total = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(
                    """
                    SELECT r.id, r.user_id, r.parcel_id, r.property_address,
                           sc.record_type, sc.county, sc.state
                    FROM results r
                    JOIN jobs j ON j.id = r.job_id
                    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                    WHERE r.id > CAST(:last_id AS uuid)
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
                # 2026-06-12: county/state-scoped key — context from the join.
                key = compute_property_key(
                    row.parcel_id, row.property_address, row.county, row.state
                )
                if not key or not row.record_type:
                    continue
                k = (str(row.user_id), row.record_type, key)
                cur = agg.get(k)
                if cur is None:
                    agg[k] = {"parcel_id": row.parcel_id, "property_address": row.property_address, "count": 1}
                else:
                    cur["count"] += 1
            # Chunked multi-row upsert (mirrors live _upsert_property_membership)
            # — per-key INSERTs over a remote prod connection cost a round-trip
            # each. Pre-aggregated by key in `agg`, so no key repeats within a
            # chunk (avoids "cannot affect row a second time").
            items = sorted(agg.items())
            for i in range(0, len(items), 500):
                chunk = items[i:i + 500]
                values_sql = ",".join(
                    f"(:uid_{k}, :rt_{k}, :pk_{k}, :pid_{k}, :addr_{k}, :cnt_{k}, NOW(), NOW())"
                    for k in range(len(chunk))
                )
                params: dict = {}
                for k, ((uid, rt, key), v) in enumerate(chunk):
                    params[f"uid_{k}"] = uid
                    params[f"rt_{k}"] = rt
                    params[f"pk_{k}"] = key
                    params[f"pid_{k}"] = v["parcel_id"]
                    params[f"addr_{k}"] = v["property_address"]
                    params[f"cnt_{k}"] = v["count"]
                db.execute(
                    text(
                        f"""
                        INSERT INTO property_list_membership
                            (user_id, record_type, property_key, parcel_id,
                             property_address, sighting_count, first_seen_at, last_seen_at)
                        VALUES {values_sql}
                        ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                            sighting_count = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                            parcel_id = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                            property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
                        """
                    ),
                    params,
                )
            db.commit()
            total += len(rows)
            _log.info("backfilled through result id %s (%d rows scanned)", last_id, total)
    _log.info("done — %d result rows scanned", total)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    run(ap.parse_args().batch)
