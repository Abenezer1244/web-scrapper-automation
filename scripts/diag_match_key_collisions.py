"""READ-ONLY: does the ingest match key actually collide in production?

Codex flagged that the webhook ingest matches Tracerfy's result CSV to our
pending rows on (property_address, city, state) alone. If two pending rows in
ONE batch share that key but describe different owners, both receive every
matching CSV row and the last one wins -- putting one person's phone/email on
another person's lead.

Before changing the match key (which is delicate: ADVANCED traces deliberately
send no name and get back whichever owner Tracerfy identifies, so keying on name
would break them), measure whether the collision is real.

Writes nothing.

Run:
    railway run --service worker python scripts/diag_match_key_collisions.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import text

    from src.db.session import system_sync_session

    with system_sync_session() as db:
        print("== Collisions WITHIN a single Tracerfy batch (the dangerous case) ==")
        rows = db.execute(text("""
            SELECT tracerfy_queue_id,
                   lower(trim(property_address)) AS addr,
                   lower(trim(coalesce(city,'')))  AS city,
                   upper(trim(coalesce(state,''))) AS state,
                   COUNT(*) AS n_rows,
                   COUNT(DISTINCT user_id) AS n_users,
                   COUNT(DISTINCT coalesce(first_name,'') || '|' ||
                                  coalesce(last_name,'')) AS n_names,
                   COUNT(DISTINCT result_id) AS n_results
            FROM pending_skip_trace_rows
            WHERE tracerfy_queue_id IS NOT NULL
            GROUP BY 1,2,3,4
            HAVING COUNT(*) > 1
            ORDER BY n_rows DESC
            LIMIT 30
        """)).fetchall()
        if not rows:
            print("  NONE — no batch ever contained two rows sharing a match key.")
        else:
            print(f"  {'queue':<10} {'rows':>5} {'users':>6} {'names':>6} {'results':>8}  address")
            for r in rows:
                print(f"  {r.tracerfy_queue_id:<10} {r.n_rows:>5} {r.n_users:>6} "
                      f"{r.n_names:>6} {r.n_results:>8}  {r.addr[:44]!r}")

        n_collisions = db.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT 1 FROM pending_skip_trace_rows
                WHERE tracerfy_queue_id IS NOT NULL
                GROUP BY tracerfy_queue_id, lower(trim(property_address)),
                         lower(trim(coalesce(city,''))), upper(trim(coalesce(state,'')))
                HAVING COUNT(*) > 1
            ) x
        """)).scalar()
        n_diff_names = db.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT 1 FROM pending_skip_trace_rows
                WHERE tracerfy_queue_id IS NOT NULL
                GROUP BY tracerfy_queue_id, lower(trim(property_address)),
                         lower(trim(coalesce(city,''))), upper(trim(coalesce(state,'')))
                HAVING COUNT(*) > 1
                   AND COUNT(DISTINCT coalesce(first_name,'') || '|' ||
                                      coalesce(last_name,'')) > 1
            ) x
        """)).scalar()
        n_diff_users = db.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT 1 FROM pending_skip_trace_rows
                WHERE tracerfy_queue_id IS NOT NULL
                GROUP BY tracerfy_queue_id, lower(trim(property_address)),
                         lower(trim(coalesce(city,''))), upper(trim(coalesce(state,'')))
                HAVING COUNT(*) > 1 AND COUNT(DISTINCT user_id) > 1
            ) x
        """)).scalar()
        total = db.execute(text(
            "SELECT COUNT(*) FROM pending_skip_trace_rows WHERE tracerfy_queue_id IS NOT NULL"
        )).scalar()

        print(f"\n  in-batch key groups with >1 row      : {n_collisions}")
        print(f"    ...of which carry DIFFERENT names  : {n_diff_names}  <-- the real risk")
        print(f"    ...of which span DIFFERENT tenants : {n_diff_users}")
        print(f"  total rows ever submitted            : {total}")

        print("\n== Same key across DIFFERENT batches (benign: separate ingests) ==")
        cross = db.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT 1 FROM pending_skip_trace_rows
                WHERE tracerfy_queue_id IS NOT NULL
                GROUP BY lower(trim(property_address)),
                         lower(trim(coalesce(city,''))), upper(trim(coalesce(state,'')))
                HAVING COUNT(DISTINCT tracerfy_queue_id) > 1
            ) x
        """)).scalar()
        print(f"  addresses appearing in more than one batch: {cross}")


if __name__ == "__main__":
    main()
