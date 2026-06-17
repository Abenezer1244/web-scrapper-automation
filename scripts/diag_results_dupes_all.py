"""READ-ONLY: find jobs whose results table has duplicate rows per parcel.

Symptom of the watchdog-requeue-re-insert bug: a long-enriching job re-queued by
the wall-clock watchdog re-runs run_scrape_job and appends a full second copy of
its results. Detect by rows != distinct parcel_id (parcel-bearing rows only).

    railway run --service worker python scripts/diag_results_dupes_all.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(
            text(
                """
                SELECT j.id::text AS id, j.created_at, j.status, j.retry_count,
                       sc.county, sc.state, sc.record_type,
                       count(r.id) AS rows,
                       count(DISTINCT r.parcel_id) AS parcels,
                       count(r.id) - count(DISTINCT r.parcel_id) AS dup_rows
                FROM jobs j
                JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                JOIN results r ON r.job_id = j.id
                WHERE r.parcel_id IS NOT NULL
                GROUP BY j.id, j.created_at, j.status, j.retry_count,
                         sc.county, sc.state, sc.record_type
                HAVING count(r.id) <> count(DISTINCT r.parcel_id)
                ORDER BY j.created_at DESC
                """
            )
        ).fetchall()
        if not rows:
            print("CLEAN: no parcel-bearing job has duplicate result rows.")
            return
        print(f"FOUND {len(rows)} job(s) with duplicate result rows:")
        for r in rows:
            ratio = (r.rows / r.parcels) if r.parcels else 0
            print(
                f"  {r.id} {r.created_at:%Y-%m-%d %H:%M} {r.county}/{r.state}/{r.record_type} "
                f"status={r.status} retry={r.retry_count} rows={r.rows} parcels={r.parcels} "
                f"dup={r.dup_rows} (x{ratio:.2f})"
            )


if __name__ == "__main__":
    main()
