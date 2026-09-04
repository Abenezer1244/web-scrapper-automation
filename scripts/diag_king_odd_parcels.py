"""Read-only: King results whose parcel_id is not the canonical 10 digits."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import func, select

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        rows = db.execute(
            select(Result.id, Result.parcel_id, Result.party_name, Result.property_address,
                   Result.mailing_address, Result.legal_description, Result.job_id,
                   ScraperConfig.record_type, Result.created_at)
            .join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .where(func.lower(ScraperConfig.county) == "king",
                   Result.parcel_id.isnot(None),
                   func.length(Result.parcel_id) != 10)
            .order_by(Result.created_at)
        ).all()
    for r in rows:
        print(f"{r.id} len={len(r.parcel_id)} pid={r.parcel_id!r} type={r.record_type} "
              f"party={r.party_name!r}\n   prop={r.property_address!r} mail={r.mailing_address!r}"
              f"\n   legal={(r.legal_description or '')[:120]!r}")


if __name__ == "__main__":
    main()
