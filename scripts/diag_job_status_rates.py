"""Job outcome rates over recent windows (Phase 0 — read-only).

Tells an INTERMITTENT enqueue-before-commit race (most jobs run, some get
stuck pending) apart from a TOTAL outage (no single job ever leaves pending —
e.g. a broken Celery `include` or a dead worker).

Read-only. No writes.

Run on prod env:
    railway run --service worker python scripts/diag_job_status_rates.py
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import func, select

    from src.db.models import Job
    from src.db.session import system_sync_session

    now = datetime.now(UTC)
    windows = [("24h", 24), ("72h", 72), ("7d", 168), ("30d", 720)]

    with system_sync_session() as db:
        for label, hrs in windows:
            since = now - timedelta(hours=hrs)
            rows = db.execute(
                select(Job.status, func.count())
                .where(Job.created_at >= since)
                .group_by(Job.status)
            ).all()
            total = sum(n for _, n in rows)
            print(f"== Jobs created in last {label} (total {total}) ==")
            for status, n in sorted(rows, key=lambda r: -r[1]):
                pct = (100.0 * n / total) if total else 0.0
                print(f"  {str(status):>12} : {n:>5}  ({pct:5.1f}%)")
            print()

        # Most recent terminal (done/failed) job — proves the pipeline can complete
        print("== Most recent terminal jobs (started_at proves a worker claimed it) ==")
        terminal = db.execute(
            select(Job)
            .where(Job.status.in_(("done", "failed", "cancelled")))
            .order_by(Job.created_at.desc())
            .limit(5)
        ).scalars().all()
        for j in terminal:
            print(f"  {j.id} status={j.status} started={j.started_at} "
                  f"created={j.created_at} user={j.user_id}")


if __name__ == "__main__":
    main()
