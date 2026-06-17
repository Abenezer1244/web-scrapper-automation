"""READ-ONLY: detect duplicate result rows for a job (re-claim/retry symptom).

    railway run --service worker python scripts/diag_job_dupes.py <job_id>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402


def main() -> None:
    job_id = sys.argv[1]
    with system_sync_session() as db:
        total = db.execute(
            text("SELECT count(*) FROM results WHERE job_id = :j"), {"j": job_id}
        ).scalar()
        distinct_parcel = db.execute(
            text("SELECT count(DISTINCT parcel_id) FROM results WHERE job_id = :j"),
            {"j": job_id},
        ).scalar()
        rc = db.execute(
            text("SELECT record_count, status, retry_count, started_at FROM jobs WHERE id = :j"),
            {"j": job_id},
        ).first()
        dupes = db.execute(
            text(
                """
                SELECT parcel_id, count(*) AS n FROM results
                WHERE job_id = :j AND parcel_id IS NOT NULL
                GROUP BY parcel_id HAVING count(*) > 1
                ORDER BY n DESC LIMIT 10
                """
            ),
            {"j": job_id},
        ).fetchall()
        dup_groups = db.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT parcel_id FROM results
                    WHERE job_id = :j AND parcel_id IS NOT NULL
                    GROUP BY parcel_id HAVING count(*) > 1
                ) d
                """
            ),
            {"j": job_id},
        ).scalar()
        print(f"job={job_id} status={rc.status} record_count={rc.record_count} retry_count={rc.retry_count} started={rc.started_at}")
        print(f"results rows={total} distinct_parcel_id={distinct_parcel} duplicate_parcel_groups={dup_groups}")
        if dupes:
            print("top duplicated parcels:")
            for p, n in dupes:
                print(f"   parcel={p}: {n} rows")


if __name__ == "__main__":
    main()
