"""READ-ONLY: what the deferred-mailing recovery sweep would find in production.

    railway run --service worker python scripts/diag_mailing_recovery_preview.py

Runs the sweep's OWN candidate query (imported, not retyped, so this cannot drift
from what the sweep actually selects) and reports the backlog it would work
through. Makes no requests and writes nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from sqlalchemy import text

    from src.db.session import system_sync_session
    from src.workers.mailing_recovery import _CANDIDATE_SQL, _MAX_ATTEMPTS

    with system_sync_session() as db:
        print("=== source health ===")
        row = db.execute(text(
            "SELECT status, cooldown_until, last_probe_at, last_success_at, "
            "consecutive_probe_failures, reason FROM external_source_health "
            "WHERE source_key = 'king_erealproperty'")).first()
        if row is None:
            print("  no row (healthy)")
        else:
            print(f"  status={row.status} cooldown_until={row.cooldown_until}")
            print(f"  last_probe_at={row.last_probe_at} last_success_at={row.last_success_at}")
            print(f"  probe_failures={row.consecutive_probe_failures}")
            print(f"  reason={str(row.reason)[:160]}")

        print("\n=== total deferred King backlog (all ages) ===")
        total = db.execute(text("""
            SELECT count(*) AS rows, count(DISTINCT r.parcel_id) AS parcels
            FROM results r
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE r.mailing_address IS NULL
              AND coalesce(r.enrichment_data->>'mailing_lookup_deferred','') = 'true'
              AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
        """)).first()
        print(f"  rows={total.rows}  distinct parcels={total.parcels}")

        print("\n=== by job status (only 'done' is eligible) ===")
        for r in db.execute(text("""
            SELECT j.status, count(*) AS rows
            FROM results r
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE r.mailing_address IS NULL
              AND coalesce(r.enrichment_data->>'mailing_lookup_deferred','') = 'true'
              AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
            GROUP BY 1 ORDER BY 2 DESC
        """)).all():
            print(f"  {r.status:12} {r.rows}")

        print(f"\n=== what ONE tick would select (max_attempts={_MAX_ATTEMPTS}) ===")
        rows = db.execute(text(_CANDIDATE_SQL),
                          {"max_attempts": _MAX_ATTEMPTS, "limit": 120}).all()
        print(f"  candidate rows this tick: {len(rows)}")
        print(f"  distinct parcels        : {len({r.parcel_id.strip() for r in rows})}")
        for r in rows[:5]:
            print(f"   parcel={r.parcel_id!r} user={str(r.user_id)[:8]} "
                  f"attempts={(r.enrichment_data or {}).get('mailing_recovery_attempts', 0)}")

        print("\n=== safety: do any candidates already carry contact data? ===")
        n = db.execute(text("""
            SELECT count(*) FROM results r
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE r.mailing_address IS NULL
              AND coalesce(r.enrichment_data->>'mailing_lookup_deferred','') = 'true'
              AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
              AND j.status = 'done'
              AND (r.phone IS NOT NULL OR r.email IS NOT NULL
                   OR r.skip_trace_status <> 'not_attempted')
        """)).scalar()
        print(f"  rows with phone/email/skip-trace already set: {n}")
        print("  (the sweep never reads or writes those columns; this is context only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
