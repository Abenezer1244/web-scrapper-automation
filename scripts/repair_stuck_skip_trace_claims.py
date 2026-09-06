"""Repair the skip-trace claims stranded in 'submitting', and report orphan queues.

WHY THIS EXISTS
    The dispatcher commits a durable claim BEFORE the Tracerfy POST and never
    auto-resubmits an unknown outcome, because re-sending a batch Tracerfy
    already accepted pays for it twice. When an outcome was never learned the
    rows parked in 'submitting' waiting for a human -- and the ops alert asking
    for that human went nowhere while OPS_ALERT_EMAIL was unset. Production
    accumulated 637 such rows across 15 jobs and 3 users, stuck up to four days,
    every one of them reading "Processing" in the UI.

    src/workers/skip_trace_dispatcher.py::_reconcile_stale_claims now does this
    automatically on every tick. This script is the ONE-OFF for the backlog that
    accrued before that shipped, and the reporter for the orphaned remote queues
    that predate the bookkeeping guard.

SAFETY
    Dry-run by default -- prints what it WOULD do and exits. Pass --apply to
    write. Uses the same conservative predicate as the live reconciler
    (match_remote_queue): it releases a claim ONLY when Tracerfy's own queue
    list proves no queue was ever created for it, adopts only an unambiguous
    single match, and refuses anything else. It NEVER resubmits.

    A released row returns to 'queued' and its Result to 'not_attempted', so the
    next dispatcher tick sends it normally. Nothing is deleted, no contact data
    is touched, and no usage counter is reset.

Run:
    railway run --service worker python scripts/repair_stuck_skip_trace_claims.py
    railway run --service worker python scripts/repair_stuck_skip_trace_claims.py --apply
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APPLY = "--apply" in sys.argv


def main():
    from sqlalchemy import func, select, text, update

    from src.db.models import PendingSkipTraceRow, Result, SkipTraceQueue
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import fetch_queues
    from src.workers.skip_trace_dispatcher import match_remote_queue

    mode = "APPLY" if APPLY else "DRY RUN"
    print(f"== Stuck skip-trace claim repair [{mode}] ==\n")

    remote = fetch_queues()
    print(f"Tracerfy reports {len(remote)} queues on this account.\n")

    with system_sync_session() as db:
        known = {r[0] for r in db.execute(select(SkipTraceQueue.tracerfy_queue_id))}

        groups = db.execute(
            select(
                PendingSkipTraceRow.submitted_at,
                PendingSkipTraceRow.trace_type,
                func.count().label("n"),
                func.count(func.distinct(PendingSkipTraceRow.user_id)).label("users"),
                func.count(func.distinct(PendingSkipTraceRow.job_id)).label("jobs"),
            )
            .where(
                PendingSkipTraceRow.status == "submitting",
                PendingSkipTraceRow.submitted_at.isnot(None),
            )
            .group_by(PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.trace_type)
            .order_by(PendingSkipTraceRow.submitted_at)
        ).all()

        if not groups:
            print("No claims stuck in 'submitting'. Nothing to repair.")
        total_released = total_adopted = total_refused = 0

        for claim_time, trace_type, n, users, jobs in groups:
            verdict, queue = match_remote_queue(remote, claim_time, trace_type, n, known)
            head = (
                f"  claimed {claim_time}  {trace_type:<9} rows={n:<5} "
                f"users={users} jobs={jobs}"
            )
            if verdict == "none":
                print(f"{head}  -> RELEASE (no Tracerfy queue: never accepted, never charged)")
                total_released += n
                if APPLY:
                    rows = db.execute(
                        select(PendingSkipTraceRow).where(
                            PendingSkipTraceRow.status == "submitting",
                            PendingSkipTraceRow.submitted_at == claim_time,
                            PendingSkipTraceRow.trace_type == trace_type,
                        )
                    ).scalars().all()
                    db.execute(
                        update(PendingSkipTraceRow)
                        .where(PendingSkipTraceRow.id.in_([r.id for r in rows]))
                        .values(status="queued", submitted_at=None)
                    )
                    # Return the lead to 'not_attempted' so the enqueue path can
                    # pick it up again. Guarded on the two in-flight states so a
                    # lead that has since been traced by another job is untouched.
                    db.execute(
                        update(Result)
                        .where(
                            Result.id.in_([r.result_id for r in rows]),
                            Result.skip_trace_status.in_(("queued", "submitted")),
                        )
                        .values(skip_trace_status="not_attempted")
                    )
                    db.commit()
            elif verdict == "one":
                print(
                    f"{head}  -> ADOPT queue {queue['id']} "
                    f"(uploaded={queue.get('rows_uploaded')}, "
                    f"credits={queue.get('credits_deducted')})"
                )
                total_adopted += n
                if APPLY:
                    print("       (adoption is left to the live reconciler — it also "
                          "re-drives ingest)")
            else:
                print(f"{head}  -> REFUSE (ambiguous: several queues fit; needs a human)")
                total_refused += n

        print(
            f"\n  release={total_released}  adopt={total_adopted}  refuse={total_refused}"
        )

        print("\n== Orphaned Tracerfy queues (charged, never recorded locally) ==")
        print("   Their completion webhooks hit the ingest's 'unknown_queue' no-op,")
        print("   so the paid results were never applied to any lead.\n")
        orphans = [q for q in remote if q.get("id") not in known]
        if not orphans:
            print("   None.")
        else:
            rows_lost = sum(q.get("rows_uploaded") or 0 for q in orphans)
            credits_lost = sum(q.get("credits_deducted") or 0 for q in orphans)
            print(f"   {'id':<10} {'created_at':<30} {'rows':>6} {'credits':>8}  type")
            for q in sorted(orphans, key=lambda x: x.get("id", 0), reverse=True):
                print(
                    f"   {q.get('id'):<10} {str(q.get('created_at')):<30} "
                    f"{str(q.get('rows_uploaded')):>6} "
                    f"{str(q.get('credits_deducted')):>8}  {q.get('trace_type')}"
                )
            print(f"\n   {len(orphans)} queues, {rows_lost} rows, {credits_lost} credits.")
            print(
                "   These are REPORTED ONLY. Their pending rows no longer exist in a\n"
                "   claimable state, so adopting them would attach results to leads that\n"
                "   have since been traced or superseded. Recovering them is a separate,\n"
                "   deliberate decision -- not a side effect of this repair."
            )

        print("\n== Post-state ==")
        for r in db.execute(text(
            "SELECT status, COUNT(*) n FROM pending_skip_trace_rows "
            "GROUP BY status ORDER BY n DESC"
        )):
            print(f"   {r.status:<12} {r.n}")

    if not APPLY:
        print("\nDRY RUN — nothing written. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    main()
