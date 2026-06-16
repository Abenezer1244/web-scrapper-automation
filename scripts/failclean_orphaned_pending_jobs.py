"""Fail-clean orphaned `pending` jobs (Phase 1 cleanup — guarded write).

Context: the single-job create path enqueued the Celery message INSIDE the
request transaction (before get_db's teardown commit). When a worker won the
race it ran the atomic claim before the row was committed, got rowcount=0 and
bailed, leaving the row stuck in `pending` forever (started_at stays NULL,
retry_count stays 0; the watchdog deliberately skips fresh rc=0 pending). These
are dead — the message is long gone — so we mark them `failed` with a clear,
non-leaking reason. Users re-create any scrape they still want.

Safety:
  * Targets ONLY genuine orphans: status='pending' AND started_at IS NULL AND
    retry_count = 0. (A job a worker has claimed has started_at set.)
  * The UPDATE repeats the same predicate as a compare-and-set, so a row a
    worker claims between the SELECT and the UPDATE is left untouched.
  * Raw parameterized UPDATE — no ORM events, no Celery side-effects, no
    billing/delivery hooks fire.
  * Dry-run by default. Pass --commit to write.

Run on prod env:
    railway run --service worker python scripts/failclean_orphaned_pending_jobs.py            # dry-run
    railway run --service worker python scripts/failclean_orphaned_pending_jobs.py --commit    # apply
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REASON = (
    "Job was orphaned in 'pending' and never started: the create request's "
    "queue message raced ahead of the database commit, so no worker could "
    "claim it. Closed by maintenance cleanup — please re-create this scrape "
    "to run it again."
)


def main():
    commit = "--commit" in sys.argv

    from sqlalchemy import text

    from src.db.session import system_sync_session

    select_sql = text(
        """
        SELECT count(*) AS n,
               min(created_at) AS oldest,
               max(created_at) AS newest,
               count(DISTINCT user_id) AS users
        FROM jobs
        WHERE status = 'pending'
          AND started_at IS NULL
          AND retry_count = 0
        """
    )
    update_sql = text(
        """
        UPDATE jobs
        SET status = 'failed',
            error_message = :reason,
            finished_at = now()
        WHERE status = 'pending'
          AND started_at IS NULL
          AND retry_count = 0
        """
    )

    with system_sync_session() as db:
        row = db.execute(select_sql).one()
        print("== Orphaned pending candidates ==")
        print(f"  count          : {row.n}")
        print(f"  distinct users : {row.users}")
        print(f"  oldest created : {row.oldest}")
        print(f"  newest created : {row.newest}")
        print()

        if row.n == 0:
            print("  Nothing to do.")
            return

        if not commit:
            print("  DRY-RUN — no changes written. Re-run with --commit to apply.")
            return

        result = db.execute(update_sql, {"reason": REASON})
        db.commit()
        print(f"  COMMITTED — marked {result.rowcount} job(s) as failed.")

        # Re-check what (if anything) is still active
        remaining = db.execute(select_sql).one()
        print(f"  remaining orphaned pending: {remaining.n}")


if __name__ == "__main__":
    main()
