"""READ-ONLY audit: did every submitted skip-trace row get reconciled?

The webhook ingest matches Tracerfy's result CSV back to our pending rows by an
EXACT (address, city, state) string compare. If Tracerfy echoes a normalized /
USPS-standardized address, the match silently misses: the pending row stays
'submitted' forever, the Result never leaves 'submitted' ("Processing" in the
UI), and the row is never counted for billing (report_usage_from_webhook only
counts status='completed').

This script measures whether that is happening in production. It writes nothing.

Run:
    railway run --service worker python scripts/diag_skip_trace_match_audit.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import text

    from src.db.session import system_sync_session

    with system_sync_session() as db:
        print("== pending_skip_trace_rows by status ==")
        for r in db.execute(text(
            "SELECT status, COUNT(*) n FROM pending_skip_trace_rows "
            "GROUP BY status ORDER BY n DESC"
        )):
            print(f"  {r.status:<12} {r.n}")

        print("\n== skip_trace_queues by status ==")
        for r in db.execute(text(
            "SELECT status, COUNT(*) n FROM skip_trace_queues "
            "GROUP BY status ORDER BY n DESC"
        )):
            print(f"  {r.status:<12} {r.n}")

        print("\n== THE SMOKING GUN: rows left 'submitted' on a COMPLETED queue ==")
        print("   (queue finished + webhook ingested, but this row never matched)")
        rows = db.execute(text("""
            SELECT q.tracerfy_queue_id,
                   q.trace_type,
                   q.completed_at,
                   q.rows_uploaded,
                   COUNT(*) FILTER (WHERE p.status = 'submitted') AS unmatched,
                   COUNT(*) FILTER (WHERE p.status = 'completed') AS matched,
                   COUNT(*) AS total
            FROM skip_trace_queues q
            JOIN pending_skip_trace_rows p
              ON p.tracerfy_queue_id = q.tracerfy_queue_id
            WHERE q.status IN ('completed', 'billed')
            GROUP BY q.tracerfy_queue_id, q.trace_type, q.completed_at, q.rows_uploaded
            HAVING COUNT(*) FILTER (WHERE p.status = 'submitted') > 0
            ORDER BY q.completed_at DESC NULLS LAST
            LIMIT 25
        """)).fetchall()
        if not rows:
            print("  NONE — every row on every completed queue reconciled.")
        else:
            print(f"  {'queue':<10} {'type':<9} {'unmatched':>9} {'matched':>8} "
                  f"{'total':>6}  completed_at")
            for r in rows:
                print(f"  {r.tracerfy_queue_id:<10} {r.trace_type:<9} "
                      f"{r.unmatched:>9} {r.matched:>8} {r.total:>6}  {r.completed_at}")

        tot = db.execute(text("""
            SELECT COUNT(*) n
            FROM skip_trace_queues q
            JOIN pending_skip_trace_rows p
              ON p.tracerfy_queue_id = q.tracerfy_queue_id
            WHERE q.status IN ('completed','billed') AND p.status = 'submitted'
        """)).scalar()
        print(f"\n  TOTAL unmatched rows across all completed queues: {tot}")

        print("\n== Result rows stranded at 'submitted'/'queued' (UI: 'Processing') ==")
        for r in db.execute(text("""
            SELECT skip_trace_status, COUNT(*) n,
                   MIN(created_at) oldest, MAX(created_at) newest
            FROM results
            WHERE skip_trace_status IN ('queued','submitted')
            GROUP BY skip_trace_status
        """)):
            print(f"  {r.skip_trace_status:<12} {r.n:<7} oldest={r.oldest} newest={r.newest}")

        print("\n== Sample unmatched rows: what WE sent (address/city/state) ==")
        print("   (compare against the queue's result CSV to see Tracerfy's echo)")
        for r in db.execute(text("""
            SELECT p.tracerfy_queue_id, p.property_address, p.city, p.state, p.zip
            FROM pending_skip_trace_rows p
            JOIN skip_trace_queues q ON q.tracerfy_queue_id = p.tracerfy_queue_id
            WHERE q.status IN ('completed','billed') AND p.status = 'submitted'
            ORDER BY p.enqueued_at DESC
            LIMIT 15
        """)):
            print(f"  q={r.tracerfy_queue_id:<8} {r.property_address!r} | "
                  f"{r.city!r} | {r.state!r} | {r.zip!r}")

        print("\n== Billing sanity: rows counted vs rows submitted per queue ==")
        for r in db.execute(text("""
            SELECT q.tracerfy_queue_id, q.rows_uploaded, q.credits_deducted,
                   COUNT(*) FILTER (WHERE p.status='completed') AS billed_rows,
                   COUNT(*) AS submitted_rows
            FROM skip_trace_queues q
            JOIN pending_skip_trace_rows p
              ON p.tracerfy_queue_id = q.tracerfy_queue_id
            WHERE q.status IN ('completed','billed')
            GROUP BY q.tracerfy_queue_id, q.rows_uploaded, q.credits_deducted
            ORDER BY q.tracerfy_queue_id DESC
            LIMIT 15
        """)):
            print(f"  q={r.tracerfy_queue_id:<8} uploaded={r.rows_uploaded:<6} "
                  f"credits={r.credits_deducted:<6} billed_rows={r.billed_rows:<6} "
                  f"submitted_rows={r.submitted_rows}")

        print("\n== Phone storage format (are we persisting raw provider strings?) ==")
        for r in db.execute(text("""
            SELECT phone, COUNT(*) n FROM results
            WHERE phone IS NOT NULL AND phone <> ''
              AND phone !~ '^[0-9]{10}$'
            GROUP BY phone ORDER BY n DESC LIMIT 10
        """)):
            print(f"  {r.phone!r}  x{r.n}")
        nonraw = db.execute(text(
            "SELECT COUNT(*) FROM results WHERE phone IS NOT NULL AND phone <> '' "
            "AND phone !~ '^[0-9]{10}$'"
        )).scalar()
        allp = db.execute(text(
            "SELECT COUNT(*) FROM results WHERE phone IS NOT NULL AND phone <> ''"
        )).scalar()
        print(f"  non-bare-10-digit phones: {nonraw} / {allp}")

        print("\n== Same phone on multiple DISTINCT properties (contamination check) ==")
        for r in db.execute(text("""
            SELECT phone,
                   COUNT(DISTINCT property_address) addrs,
                   COUNT(*) rows
            FROM results
            WHERE phone IS NOT NULL AND phone <> ''
            GROUP BY phone
            HAVING COUNT(DISTINCT property_address) > 3
            ORDER BY addrs DESC LIMIT 10
        """)):
            print(f"  {r.phone!r} -> {r.addrs} distinct addresses, {r.rows} rows")


if __name__ == "__main__":
    main()
