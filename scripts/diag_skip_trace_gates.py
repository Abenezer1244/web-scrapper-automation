"""For recent jobs, print the two enqueue gates: config.skip_trace_enabled
and user.plan. Read-only.

    railway run --service worker python scripts/diag_skip_trace_gates.py
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOURS = int(os.environ.get("HOURS", "36"))


def main():
    from sqlalchemy import func, select

    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import system_sync_session

    since = datetime.now(UTC) - timedelta(hours=HOURS)

    with system_sync_session() as db:
        jobs = db.execute(
            select(Job).where(Job.created_at >= since)
            .order_by(Job.created_at.desc())
        ).scalars().all()

        print(f"{len(jobs)} jobs in last {HOURS}h\n")
        for j in jobs:
            cfg = db.get(ScraperConfig, j.scraper_config_id)
            user = db.get(User, j.user_id)
            res_count = db.execute(
                select(func.count()).select_from(Result)
                .where(Result.job_id == j.id)
            ).scalar_one()
            st_enabled = getattr(cfg, "skip_trace_enabled", "<no-col>") if cfg else "<no-cfg>"
            cfg_name = getattr(cfg, "name", "?") if cfg else "?"
            county = getattr(cfg, "county", "?") if cfg else "?"
            rtype = getattr(cfg, "record_type", "?") if cfg else "?"
            plan = (user.plan if user else "?")
            print(f"job={j.id[:8]} '{cfg_name}' {county}/{rtype} "
                  f"results={res_count} | config.skip_trace_enabled={st_enabled} "
                  f"| user.plan={plan} | user={user.email if user else '?'}")


if __name__ == "__main__":
    main()
