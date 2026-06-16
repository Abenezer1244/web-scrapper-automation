"""Diagnose stuck/active scrape jobs (Phase 0 — read-only).

Reports every Job in a non-terminal status with its age, retry_count, and
started_at so we can tell an orphaned `pending` (enqueue-before-commit race)
apart from a genuinely long-running scrape or a worker/beat outage.

Read-only. No writes. Filters nothing out — shows all tenants because this is
an operator diagnostic (run by an admin with the prod DSN, not via the API).

Run on prod env:
    railway run --service worker python scripts/diag_stuck_jobs.py
"""

import os
import sys
from collections import Counter
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTIVE = ("pending", "queued", "probing", "scraping", "enriching")


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def main():
    from sqlalchemy import select

    from src.db.models import Job
    from src.db.session import system_sync_session

    now = datetime.now(UTC)
    print(f"== Active jobs as of {now.isoformat()} ==")
    print(f"   (statuses: {', '.join(ACTIVE)})\n")

    with system_sync_session() as db:
        jobs = db.execute(
            select(Job)
            .where(Job.status.in_(ACTIVE))
            .order_by(Job.created_at.desc())
        ).scalars().all()

        if not jobs:
            print("  <none — no jobs in an active status>")
            return

        # Summary breakdowns
        by_status = Counter(str(j.status) for j in jobs)
        by_user = Counter(str(j.user_id) for j in jobs)
        none_started = sum(1 for j in jobs if j.started_at is None)
        rc0 = sum(1 for j in jobs if getattr(j, "retry_count", None) == 0)
        # orphan candidate: pending + rc0 + no started_at + older than 10 min
        orphan_old = sum(
            1 for j in jobs
            if str(j.status) == "pending"
            and getattr(j, "retry_count", None) == 0
            and j.started_at is None
            and _aware(j.created_at) is not None
            and (now - _aware(j.created_at)).total_seconds() > 600
        )

        print("== Summary ==")
        print(f"  total active           : {len(jobs)}")
        print(f"  by status              : {dict(by_status)}")
        print(f"  started_at IS NULL     : {none_started}")
        print(f"  retry_count == 0       : {rc0}")
        print(f"  orphan-candidates(>10m): {orphan_old}")
        print(f"  distinct users         : {len(by_user)}")
        print(f"  by user                : {dict(by_user)}")
        print()

        # Oldest + newest few for context
        print("== Newest 5 active ==")
        for j in jobs[:5]:
            age = now - _aware(j.created_at) if j.created_at else None
            print(f"  {j.id} status={j.status} rc={getattr(j,'retry_count','?')} "
                  f"started={j.started_at} created={j.created_at} age={age} "
                  f"user={j.user_id}")
        print("\n== Oldest 5 active ==")
        for j in jobs[-5:]:
            age = now - _aware(j.created_at) if j.created_at else None
            print(f"  {j.id} status={j.status} rc={getattr(j,'retry_count','?')} "
                  f"started={j.started_at} created={j.created_at} age={age} "
                  f"user={j.user_id}")


if __name__ == "__main__":
    main()
