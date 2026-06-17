"""Protective monitor for ONE long enrich job (prevents the 65min-kill / 70min-requeue dup).

Polls every 120s. Exits when the job reaches a terminal state. If the job is still
non-terminal at >= CANCEL_AT_MIN minutes elapsed (default 63, just under the 65min
Celery hard limit), it CAS-cancels the job to 'cancelled' so the watchdog can't
re-queue it and double the results.

    railway run --service worker python scripts/monitor_king_job_guard.py <job_id>
    railway run --service worker python scripts/monitor_king_job_guard.py <job_id> --dry-run
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_TERMINAL = ("done", "failed", "cancelled")
CANCEL_AT_MIN = 63.0
POLL_S = 120
REASON = ("Protective cancel: enrich exceeded safe window (<65min Celery hard limit); "
          "prevents watchdog re-queue duplication. Verification stands on clean snapshot.")


def _snapshot(db, job_id):
    return db.execute(
        text(
            "SELECT status, retry_count, started_at, "
            "EXTRACT(EPOCH FROM (now() - started_at)) AS elapsed_s FROM jobs WHERE id = :j"
        ),
        {"j": job_id},
    ).first()


def _fill(db, job_id):
    return db.execute(
        text("SELECT count(*) AS total, count(mailing_address) AS mail, "
             "count(phone) AS phone FROM results WHERE job_id = :j"),
        {"j": job_id},
    ).first()


def main() -> None:
    job_id = sys.argv[1]
    dry = "--dry-run" in sys.argv
    while True:
        with system_sync_session() as db:
            r = _snapshot(db, job_id)
            if not r:
                print(f"NOT FOUND: {job_id}", flush=True)
                return
            elapsed_min = float(r.elapsed_s or 0) / 60.0
            f = _fill(db, job_id)
            print(f"[poll] status={r.status} retry={r.retry_count} elapsed={elapsed_min:.1f}min "
                  f"mailing={f.mail}/{f.total} phone={f.phone}", flush=True)

            if r.status in _TERMINAL:
                print(f"TERMINAL: {r.status} — monitor exiting.", flush=True)
                return

            if elapsed_min >= CANCEL_AT_MIN:
                if dry:
                    print(f"DRY-RUN: would CANCEL at {elapsed_min:.1f}min (status={r.status}).",
                          flush=True)
                    return
                res = db.execute(
                    text(
                        "UPDATE jobs SET status='cancelled', error_message=:reason, "
                        "finished_at=now() WHERE id = :j "
                        "AND status NOT IN ('done','failed','cancelled')"
                    ),
                    {"j": job_id, "reason": REASON},
                )
                db.commit()
                after = db.execute(
                    text("SELECT status FROM jobs WHERE id = :j"), {"j": job_id}
                ).scalar()
                print(f"PROTECTIVE CANCEL at {elapsed_min:.1f}min — rows={res.rowcount} "
                      f"status_now={after}", flush=True)
                return
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
