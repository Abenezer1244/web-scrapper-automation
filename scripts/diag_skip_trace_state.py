"""Diagnose why recent leads have no phone/email.

Read-only. Reports, for results created in the last N hours:
  - count of Result rows by skip_trace_status
  - count of those with a non-null phone/email
  - the pending_skip_trace_rows queue broken down by status
  - the most recent skip_trace_queue (Tracerfy submission) records
  - the relevant settings flags (SKIP_TRACE_ENABLED, token presence)

Run on prod env:
    railway run --service worker python scripts/diag_skip_trace_state.py
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOURS = int(os.environ.get("HOURS", "36"))


def main():
    from sqlalchemy import func, select

    from src.config import settings
    from src.db.models import (
        Job,
        PendingSkipTraceRow,
        Result,
        SkipTraceQueue,
    )
    from src.db.session import system_sync_session

    since = datetime.now(UTC) - timedelta(hours=HOURS)
    print(f"== Window: results since {since.isoformat()} ({HOURS}h) ==\n")

    print("== Settings ==")
    print(f"  SKIP_TRACE_ENABLED      : {settings.SKIP_TRACE_ENABLED}")
    print(f"  TRACERFY_API_TOKEN set  : {bool(settings.TRACERFY_API_TOKEN)}")
    print(f"  AUTO flag (if any)      : "
          f"{getattr(settings, 'SKIP_TRACE_AUTO', '<n/a>')}")
    print()

    with system_sync_session() as db:
        # Results by skip_trace_status in window
        print("== Result rows by skip_trace_status (window) ==")
        rows = db.execute(
            select(Result.skip_trace_status, func.count())
            .where(Result.created_at >= since)
            .group_by(Result.skip_trace_status)
        ).all()
        total = 0
        for status, n in rows:
            total += n
            print(f"  {str(status):>12} : {n}")
        print(f"  {'TOTAL':>12} : {total}")
        print()

        # How many have actual contact data
        with_phone = db.execute(
            select(func.count()).select_from(Result)
            .where(Result.created_at >= since, Result.phone.isnot(None))
        ).scalar_one()
        with_email = db.execute(
            select(func.count()).select_from(Result)
            .where(Result.created_at >= since, Result.email.isnot(None))
        ).scalar_one()
        print(f"  with phone != NULL : {with_phone}")
        print(f"  with email != NULL : {with_email}")
        print()

        # Pending queue by status (all-time — small table)
        print("== pending_skip_trace_rows by status (all) ==")
        prows = db.execute(
            select(PendingSkipTraceRow.status, func.count())
            .group_by(PendingSkipTraceRow.status)
        ).all()
        if not prows:
            print("  <empty table>")
        for status, n in prows:
            print(f"  {str(status):>12} : {n}")
        print()

        # Pending rows enqueued in window
        pend_window = db.execute(
            select(func.count()).select_from(PendingSkipTraceRow)
            .where(PendingSkipTraceRow.enqueued_at >= since)
        ).scalar_one()
        print(f"  pending rows enqueued in window : {pend_window}")
        print()

        # Recent Tracerfy submissions
        print("== Recent skip_trace_queue (Tracerfy submissions), last 10 ==")
        queues = db.execute(
            select(SkipTraceQueue)
            .order_by(SkipTraceQueue.submitted_at.desc())
            .limit(10)
        ).scalars().all()
        if not queues:
            print("  <no submissions ever>")
        for q in queues:
            print(f"  queue_id={q.tracerfy_queue_id} status={q.status} "
                  f"type={q.trace_type} rows={q.rows_uploaded} "
                  f"credits={q.credits_deducted} submitted={q.submitted_at}")
        print()

        # Per-job breakdown in window: did the job request skip trace?
        print("== Jobs in window (skip_trace flags) ==")
        jobs = db.execute(
            select(Job)
            .where(Job.created_at >= since)
            .order_by(Job.created_at.desc())
            .limit(20)
        ).scalars().all()
        for j in jobs:
            res_count = db.execute(
                select(func.count()).select_from(Result)
                .where(Result.job_id == j.id)
            ).scalar_one()
            hit_count = db.execute(
                select(func.count()).select_from(Result)
                .where(Result.job_id == j.id,
                       Result.skip_trace_status == "hit")
            ).scalar_one()
            st_req = getattr(j, "skip_trace_requested", "<n/a>")
            st_enabled = getattr(j, "skip_trace_enabled", "<n/a>")
            print(f"  job={j.id} user={j.user_id} county={getattr(j,'county','?')} "
                  f"type={getattr(j,'record_type','?')} status={j.status} "
                  f"results={res_count} hits={hit_count} "
                  f"st_requested={st_req} st_enabled={st_enabled}")


if __name__ == "__main__":
    main()
