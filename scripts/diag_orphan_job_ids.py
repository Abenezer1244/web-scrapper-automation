"""READ-ONLY: list the exact orphaned 'pending' job IDs + current UTC time.

Surgical fail-clean (Codex P1) needs the exact incident-set IDs, not a broad
predicate. This prints them plus now() so a created_at cutoff can be proven to
sit in the past relative to any fresh job.

    railway run --service worker python scripts/diag_orphan_job_ids.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402


def main() -> None:
    with system_sync_session() as db:
        print("NOW_UTC:", db.execute(text("SELECT now()")).scalar())
        rows = db.execute(
            text(
                """
                SELECT id::text, user_id::text, scraper_config_id::text,
                       created_at, status, started_at, retry_count
                FROM jobs
                WHERE status = 'pending' AND started_at IS NULL AND retry_count = 0
                ORDER BY created_at
                """
            )
        ).fetchall()
        print("ORPHAN_COUNT:", len(rows))
        for r in rows:
            print(
                f"  id={r[0]} user={r[1]} cfg={r[2]} "
                f"created={r[3]} status={r[4]} started={r[5]} retry={r[6]}"
            )


if __name__ == "__main__":
    main()
