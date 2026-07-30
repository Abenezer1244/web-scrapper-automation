"""READ-ONLY: what are the 12 renamed configs actually doing?

Decides whether they are live scrapers worth renaming to something meaningful, or
inactive test leftovers. Reads the audit trail written by the backfill.

    railway run --service worker python scripts/diag_renamed_config_usage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session


def main() -> None:
    with system_sync_session() as db:
        ids = [
            r.detail.split(":")[0]
            for r in db.execute(
                text(
                    "SELECT detail FROM audit_events "
                    "WHERE event = 'scraper_config.name_backfilled' ORDER BY created_at"
                )
            ).all()
        ]
        print(f"=== {len(ids)} configs renamed by the backfill ===\n")

        rows = db.execute(
            text(
                "SELECT sc.id::text AS id, sc.name, sc.active, sc.created_at, "
                "sc.schedule::text AS schedule, sb.name AS batch_name, "
                "(SELECT count(*) FROM jobs j WHERE j.scraper_config_id = sc.id) AS job_count, "
                "(SELECT max(j.created_at) FROM jobs j WHERE j.scraper_config_id = sc.id) AS last_job, "
                "(SELECT coalesce(sum(j.record_count), 0) FROM jobs j "
                " WHERE j.scraper_config_id = sc.id AND j.status = 'done') AS total_records "
                "FROM scraper_configs sc LEFT JOIN scraper_batches sb ON sb.id = sc.batch_id "
                "WHERE sc.id = ANY(CAST(:ids AS uuid[])) "
                "ORDER BY sc.active DESC, sc.created_at"
            ),
            {"ids": ids},
        ).all()

    live = idle = 0
    for r in rows:
        freq = "recurring" if '"frequency"' in (r.schedule or "") and "manual" not in (r.schedule or "") else "manual"
        flag = "ACTIVE " if r.active else "inactive"
        if r.active and r.job_count:
            live += 1
        else:
            idle += 1
        print(f"[{flag}] {r.name}")
        print(
            f"          jobs={r.job_count} records={r.total_records} last_job={r.last_job} "
            f"sched={freq}"
        )

    print(f"\n=== {live} active-with-jobs, {idle} inactive or never-run ===")


if __name__ == "__main__":
    main()
