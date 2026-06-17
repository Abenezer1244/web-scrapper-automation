"""READ-ONLY: latest King tax job id + status + single-copy check.

    railway run --service worker python scripts/diag_latest_king_job.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_CFG = "40333eb4-e991-499b-ae2c-563da463920a"


def main() -> None:
    with system_sync_session() as db:
        j = db.execute(
            text(
                "SELECT id::text, status, record_count, retry_count, created_at, "
                "started_at, finished_at FROM jobs WHERE scraper_config_id = :c "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"c": _CFG},
        ).first()
        if not j:
            print("NO JOBS for King config")
            return
        rows = db.execute(
            text("SELECT count(*), count(DISTINCT parcel_id) FROM results WHERE job_id = :j"),
            {"j": j.id},
        ).first()
        print(f"job_id={j.id}")
        print(f"status={j.status} record_count={j.record_count} retry_count={j.retry_count}")
        print(f"created={j.created_at} started={j.started_at} finished={j.finished_at}")
        print(f"results_rows={rows[0]} distinct_parcels={rows[1]} "
              f"single_copy={'YES' if rows[0] == rows[1] else 'NO (DUPLICATED)'}")


if __name__ == "__main__":
    main()
