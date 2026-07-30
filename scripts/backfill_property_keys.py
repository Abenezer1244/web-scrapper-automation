"""Re-key property identity to the 2026-06-12 scheme (parcel-primary,
county/state-scoped) and rebuild property_list_membership.

WHY: the old property_key hashed parcel|address together; pipelines store
different address formats for the same parcel (tax situs city+ZIP4 vs GIS
street-only), so tax lists could never overlap recorder lists. Stored keys do
not self-heal — this script is MANDATORY immediately after deploying the new
compute_property_key.

WHAT IT DOES (in order):
  1. Recompute results.property_key for EVERY row (county/state via the
     jobs -> scraper_configs join; weak identity -> NULL). Unconditional
     re-key: old-scheme values are replaced, idempotent on re-run.
  2. Rebuild property_list_membership per user, atomically (DELETE + INSERT
     in one transaction per user so readers never see a half-empty rollup).
     Aggregates are EXPLICIT (Codex P1): sighting_count = count of strong
     result rows per (key, record_type) — same per-row-sighting semantics as
     the live writer; first/last_seen = min/max(results.created_at).

Dry-run by default; --commit to write. Batched keyset scan on results.id.

Usage:
  railway run --service worker python scripts/backfill_property_keys.py            # dry-run
  railway run --service worker python scripts/backfill_property_keys.py --commit
"""
import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

# SYSTEM session (Codex P1): this is a cross-tenant maintenance re-key; a
# plain session would scan zero rows / fail writes once RLS_ENFORCE is on.
from src.db.session import system_sync_session
from src.workers.property_identity import compute_property_key

_NIL = "00000000-0000-0000-0000-000000000000"

_SCAN_SQL = text(
    """
    SELECT r.id, r.parcel_id, r.property_address, r.property_key,
           sc.county, sc.state
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.id > CAST(:last_id AS uuid)
    ORDER BY r.id
    LIMIT :batch
    """
)

_REBUILD_USERS_SQL = text("SELECT DISTINCT user_id FROM results")

_MEMBERSHIP_INSERT_SQL = text(
    """
    INSERT INTO property_list_membership
        (user_id, record_type, property_key, parcel_id, property_address,
         sighting_count, first_seen_at, last_seen_at)
    SELECT r.user_id, sc.record_type, r.property_key,
           min(r.parcel_id), min(r.property_address),
           count(*), min(r.created_at), max(r.created_at)
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.user_id = CAST(:uid AS uuid) AND r.property_key IS NOT NULL
    GROUP BY r.user_id, sc.record_type, r.property_key
    """
)


def rekey_results(batch: int, commit: bool) -> tuple[int, int]:
    last_id = _NIL
    scanned = changed = 0
    while True:
        with system_sync_session() as db:
            rows = db.execute(_SCAN_SQL, {"last_id": last_id, "batch": batch}).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            updates: list[tuple[str, str | None]] = []
            for row in rows:
                scanned += 1
                new_key = compute_property_key(
                    row.parcel_id, row.property_address, row.county, row.state
                )
                if new_key != row.property_key:
                    updates.append((str(row.id), new_key))
            if updates and commit:
                values_sql = ",".join(
                    f"(:id_{i}, CAST(:pk_{i} AS varchar))" for i in range(len(updates))
                )
                params: dict = {}
                for i, (rid, key) in enumerate(updates):
                    params[f"id_{i}"] = rid
                    params[f"pk_{i}"] = key
                # values_sql is generated placeholder names only; all values
                # are bound params — not an injection surface.
                db.execute(
                    text(
                        f"UPDATE results SET property_key = data.pk "  # noqa: S608
                        f"FROM (VALUES {values_sql}) AS data(id, pk) "
                        f"WHERE results.id = data.id::uuid"
                    ),
                    params,
                )
                db.commit()
            changed += len(updates)
            print(f"  rekey: scanned {scanned} (changed {changed}) through {last_id}")
    return scanned, changed


def rebuild_membership(commit: bool) -> int:
    with system_sync_session() as db:
        user_ids = [str(u.user_id) for u in db.execute(_REBUILD_USERS_SQL).fetchall()]
    rebuilt = 0
    for uid in user_ids:
        with system_sync_session() as db:
            if commit:
                # Atomic per user: DELETE + INSERT in one txn — a concurrent
                # overlap query sees either the old rollup or the new one,
                # never an empty one.
                db.execute(
                    text("DELETE FROM property_list_membership WHERE user_id = CAST(:u AS uuid)"),
                    {"u": uid},
                )
                db.execute(_MEMBERSHIP_INSERT_SQL, {"uid": uid})
                db.commit()
                n = db.execute(
                    text("SELECT count(*) FROM property_list_membership WHERE user_id = CAST(:u AS uuid)"),
                    {"u": uid},
                ).scalar()
            else:
                n = db.execute(
                    text(
                        "SELECT count(*) FROM (SELECT 1 FROM results r "
                        "JOIN jobs j ON j.id=r.job_id "
                        "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
                        "WHERE r.user_id = CAST(:uid AS uuid) AND r.property_key IS NOT NULL "
                        "GROUP BY r.user_id, sc.record_type, r.property_key) x"
                    ),
                    {"uid": uid},
                ).scalar()
            rebuilt += int(n or 0)
            print(f"  membership user {uid[:8]}…: {n} rows")
    return rebuilt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()
    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"[{mode}] 1/2 re-keying results.property_key …")
    scanned, changed = rekey_results(args.batch, args.commit)
    print(f"[{mode}] results: scanned {scanned}, re-keyed {changed}")
    print(f"[{mode}] 2/2 rebuilding property_list_membership …")
    total = rebuild_membership(args.commit)
    print(f"[{mode}] membership rows {'written' if args.commit else 'projected'}: {total}")
    if not args.commit:
        print("Dry-run only. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
