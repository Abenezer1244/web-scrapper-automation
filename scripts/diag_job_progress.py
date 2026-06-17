"""READ-ONLY: print one job's current status/progress (poll helper).

    railway run --service worker python scripts/diag_job_progress.py <job_id>
    railway run --service worker python scripts/diag_job_progress.py <job_id> --wait  # loop until terminal
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_TERMINAL = {"done", "failed", "cancelled"}


def _snapshot(job_id: str):
    with system_sync_session() as db:
        return db.execute(
            text(
                """
                SELECT status, record_count, error_message,
                       created_at, started_at, finished_at, now() AS now
                FROM jobs WHERE id = :id
                """
            ),
            {"id": job_id},
        ).first()


def main() -> None:
    job_id = sys.argv[1]
    wait = "--wait" in sys.argv
    while True:
        r = _snapshot(job_id)
        if not r:
            print(f"NOT FOUND: {job_id}")
            return
        print(f"[{r.now}] status={r.status} records={r.record_count}", flush=True)
        if not wait or r.status in _TERMINAL:
            print(f"created={r.created_at} started={r.started_at} finished={r.finished_at}")
            if r.error_message:
                print(f"error={r.error_message}")
            if r.status in _TERMINAL:
                print(f"TERMINAL: {r.status}")
            return
        time.sleep(30)


if __name__ == "__main__":
    main()
