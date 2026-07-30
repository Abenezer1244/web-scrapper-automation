"""READ-ONLY: exactly what a HARD delete of the 12 backfilled configs would destroy.

The product's own DELETE /scrapers/{id} is a SOFT delete (scrapers.py:399 sets
active=False, "preserves job history"), so a hard delete has no application path
and its cascade reach is not obvious. This counts every row that would go.

    railway run --service worker python scripts/diag_config_delete_blast_radius.py
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
                    "WHERE event = 'scraper_config.name_backfilled'"
                )
            ).all()
        ]
        p = {"ids": ids}
        print(f"=== target: {len(ids)} scraper_configs ===\n")

        jobs = db.execute(
            text("SELECT id::text FROM jobs WHERE scraper_config_id = ANY(CAST(:ids AS uuid[]))"),
            p,
        ).all()
        job_ids = [j.id for j in jobs]
        print(f"jobs (CASCADE)                 : {len(job_ids)}")

        if not job_ids:
            print("results (CASCADE via jobs)     : 0")
            return

        jp = {"jids": job_ids}
        leads = db.execute(
            text("SELECT count(*) AS n FROM results WHERE job_id = ANY(CAST(:jids AS uuid[]))"), jp
        ).scalar()
        print(f"results (CASCADE via jobs)     : {leads}   <-- REAL SCRAPED LEAD DATA")

        # Are any of those leads reachable from a DIFFERENT, surviving job? If a
        # lead row is unique to a doomed job, deleting is genuinely lossy.
        skip = db.execute(
            text(
                "SELECT count(*) AS n FROM results "
                "WHERE job_id = ANY(CAST(:jids AS uuid[])) "
                "AND (phones IS NOT NULL OR emails IS NOT NULL)"
            ),
            jp,
        ).scalar()
        print(f"  ...of which carry skip-trace : {skip}   <-- PAID enrichment, unrecoverable")

        for table, col in (
            ("job_logs", "job_id"),
            ("notifications", "job_id"),
            ("delivered_records", "job_id"),
            ("user_record_views", "scraper_config_id"),
        ):
            try:
                key = "jids" if col == "job_id" else "ids"
                arr = jp if col == "job_id" else p
                n = db.execute(
                    text(
                        # noqa must sit on the line ruff reports (the f-string), not on
                        # text(...) — table/col come from the literal tuple above, never input.
                        f"SELECT count(*) AS n FROM {table} "  # noqa: S608
                        f"WHERE {col} = ANY(CAST(:{key} AS uuid[]))"
                    ),
                    arr,
                ).scalar()
                print(f"{table:31}: {n}")
            except Exception as e:  # table may not exist in this schema
                print(f"{table:31}: (n/a — {type(e).__name__})")
                db.rollback()

        batches = db.execute(
            text(
                "SELECT count(DISTINCT batch_id) AS n FROM scraper_configs "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND batch_id IS NOT NULL"
            ),
            p,
        ).scalar()
        print(f"\nparent batches left childless  : {batches}  (scraper_batches rows NOT deleted)")


if __name__ == "__main__":
    main()
