"""Guarded cancel of ONE job by id (stop a watchdog re-queue loop).

Flips status -> 'cancelled' (terminal) only if currently non-terminal, via a
compare-and-set. 'cancelled' is not in the watchdog's stuck-check set, so this
stops further re-queues; the running task's _set_status CAS then refuses any
later done/deliver/bill. Dry-run by default.

    railway run --service worker python scripts/cancel_job.py <job_id>            # dry-run
    railway run --service worker python scripts/cancel_job.py <job_id> --commit    # apply
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session

_TERMINAL = ("done", "failed", "cancelled")
REASON = "Cancelled by maintenance: watchdog re-queue loop duplicating results; see incident."


def main() -> None:
    job_id = sys.argv[1]
    commit = "--commit" in sys.argv
    with system_sync_session() as db:
        r = db.execute(
            text("SELECT status, retry_count, started_at FROM jobs WHERE id = :j"), {"j": job_id}
        ).first()
        if not r:
            print(f"NOT FOUND: {job_id}")
            return
        print(f"current: status={r.status} retry_count={r.retry_count} started={r.started_at}")
        if r.status in _TERMINAL:
            print(f"already terminal ({r.status}) — nothing to do.")
            return
        if not commit:
            print("DRY-RUN — would set status=cancelled. Re-run with --commit.")
            return
        res = db.execute(
            text(
                """
                UPDATE jobs SET status='cancelled', error_message=:reason, finished_at=now()
                WHERE id = :j AND status NOT IN ('done','failed','cancelled')
                """
            ),
            {"j": job_id, "reason": REASON},
        )
        db.commit()
        print(f"COMMITTED — rows updated={res.rowcount}")
        after = db.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": job_id}).scalar()
        print(f"status now: {after}")


if __name__ == "__main__":
    main()
