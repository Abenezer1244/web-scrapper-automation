"""READ-ONLY: why are 637 pending_skip_trace_rows stuck in 'submitting'?

Reconciles our stuck claims against Tracerfy's own queue list (GET
/v1/api/queues/) to answer the question the dispatcher deliberately refuses to
guess at: did Tracerfy actually accept (and charge for) these batches?

Writes nothing. Never prints the API token.

Run:
    railway run --service worker python scripts/diag_skip_trace_stuck_claims.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import text

    from src.config import settings
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import TracerfyError, fetch_queues

    print("== Ops alerting config ==")
    print(f"  OPS_ALERT_EMAIL set : {bool(getattr(settings, 'OPS_ALERT_EMAIL', ''))}")
    print(f"  SKIP_TRACE_ENABLED  : {settings.SKIP_TRACE_ENABLED}")
    print(f"  TRACERFY token set  : {bool(settings.TRACERFY_API_TOKEN)}")

    with system_sync_session() as db:
        print("\n== Stuck 'submitting' claims: when were they claimed? ==")
        for r in db.execute(text("""
            SELECT date_trunc('hour', submitted_at) AS hr,
                   trace_type,
                   COUNT(*) n,
                   COUNT(DISTINCT user_id) users,
                   COUNT(DISTINCT job_id) jobs
            FROM pending_skip_trace_rows
            WHERE status = 'submitting'
            GROUP BY 1, 2 ORDER BY 1
        """)):
            print(f"  {r.hr}  {r.trace_type:<9} rows={r.n:<5} users={r.users} jobs={r.jobs}")

        nullsub = db.execute(text(
            "SELECT COUNT(*) FROM pending_skip_trace_rows "
            "WHERE status='submitting' AND submitted_at IS NULL"
        )).scalar()
        print(f"  claims with NULL submitted_at (invisible to stale-check): {nullsub}")

        print("\n== Do any stuck claims carry a tracerfy_queue_id? ==")
        for r in db.execute(text("""
            SELECT (tracerfy_queue_id IS NOT NULL) AS has_qid, COUNT(*) n
            FROM pending_skip_trace_rows WHERE status='submitting'
            GROUP BY 1
        """)):
            print(f"  has_queue_id={r.has_qid}: {r.n}")

        print("\n== Rows we sent with a NULL/empty state or city (Tracerfy requires both) ==")
        for r in db.execute(text("""
            SELECT status,
                   COUNT(*) FILTER (WHERE state IS NULL OR state = '') AS no_state,
                   COUNT(*) FILTER (WHERE city  IS NULL OR city  = '') AS no_city,
                   COUNT(*) AS total
            FROM pending_skip_trace_rows
            GROUP BY status ORDER BY total DESC
        """)):
            print(f"  {r.status:<12} no_state={r.no_state:<5} no_city={r.no_city:<5} total={r.total}")

    print("\n== Tracerfy account reconciliation (GET /v1/api/queues/) ==")
    if not settings.TRACERFY_API_TOKEN:
        print("  TRACERFY_API_TOKEN not set in this env — cannot reconcile.")
        return
    # Go through the hardened client, not a bare requests.get: fetch_queues()
    # requires HTTPS, SSRF-validates the host with resolve=True BEFORE the bearer
    # token is sent, disables ambient proxies and refuses redirects. A diagnostic
    # is still a process holding a live credential (Codex).
    try:
        queues = fetch_queues()
    except TracerfyError as exc:
        print(f"  queue fetch failed: {exc}")
        return
    print(f"  Tracerfy reports {len(queues)} queues on this account.")
    print(f"  {'id':<10} {'created_at':<30} {'pending':<8} {'rows':<7} {'credits':<8} type")
    for q in sorted(queues, key=lambda x: x.get("id", 0), reverse=True)[:40]:
        print(f"  {q.get('id'):<10} {str(q.get('created_at')):<30} "
              f"{str(q.get('pending')):<8} {str(q.get('rows_uploaded')):<7} "
              f"{str(q.get('credits_deducted')):<8} {q.get('trace_type')}")

    known = set()
    with system_sync_session() as db:
        known = {
            r[0] for r in db.execute(
                text("SELECT tracerfy_queue_id FROM skip_trace_queues")
            )
        }
    unknown = [q for q in queues if q.get("id") not in known]
    print(f"\n  Queues Tracerfy has that WE have no record of: {len(unknown)}")
    for q in sorted(unknown, key=lambda x: x.get("id", 0), reverse=True)[:20]:
        print(f"    id={q.get('id')} created={q.get('created_at')} "
              f"rows={q.get('rows_uploaded')} credits={q.get('credits_deducted')} "
              f"type={q.get('trace_type')} pending={q.get('pending')}")

    completed = [q for q in queues if q.get("pending") is False]
    print(
        f"\n  {len(completed)} of {len(queues)} queues are complete. A PENDING queue "
        "hides its rows_uploaded/credits_deducted, which is why the reconciler "
        "defers on one rather than reading the absence as 'never accepted'."
    )


if __name__ == "__main__":
    main()
