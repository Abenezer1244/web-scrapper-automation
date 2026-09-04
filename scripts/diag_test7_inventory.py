"""Read-only inventory of the "Test 7" scraper config, its jobs and results.

    railway run python scripts/diag_test7_inventory.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import select

    from src.db.models import Job, Result, ScraperBatch, ScraperConfig
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        cfgs = db.execute(
            select(ScraperConfig).where(ScraperConfig.name.ilike("%test 7%"))
        ).scalars().all()
        batches = db.execute(
            select(ScraperBatch).where(ScraperBatch.name.ilike("%test 7%"))
        ).scalars().all()
        print(f"configs matching 'test 7': {len(cfgs)}  batches: {len(batches)}")
        for b in batches:
            print(f"  BATCH {b.id} name={b.name!r} user={b.user_id} status={getattr(b,'status',None)}")
        for c in cfgs:
            print(f"\nCONFIG {c.id} name={c.name!r} user={c.user_id}")
            print(f"  county={c.county} state={c.state} record_type={c.record_type} batch_id={c.batch_id}")
            print(f"  doc_types={c.doc_types} active={c.active} created={c.created_at}")
            jobs = db.execute(
                select(Job).where(Job.scraper_config_id == c.id).order_by(Job.created_at)
            ).scalars().all()
            for j in jobs:
                n = db.execute(select(Result.id).where(Result.job_id == j.id)).scalars().all()
                print(f"  JOB {j.id} status={j.status} trigger={j.trigger} "
                      f"records={j.record_count} results={len(n)} "
                      f"window={j.date_from}..{j.date_to} created={j.created_at}")
    print("\ndone")


if __name__ == "__main__":
    main()
