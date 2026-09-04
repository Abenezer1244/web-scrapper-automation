"""Read-only prod scan: probate/death-cert leads whose party_name is a recorder
placeholder / filing agency, and King parcel_ids that are not 10 digits.

    railway run python scripts/diag_probate_party_scan.py
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import func, select

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        rows = db.execute(
            select(Result.id, Result.party_name, Result.heirs, Result.parcel_id,
                   ScraperConfig.county, ScraperConfig.state, ScraperConfig.record_type,
                   Result.job_id)
            .join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .where(ScraperConfig.record_type.in_(("probate", "death_certificate")))
        ).all()

    print(f"probate/death_certificate results: {len(rows)}")
    pub = re.compile(r"^\s*(?:THE\s+)?PUBLIC(?:\s+THE)?\s*$", re.IGNORECASE)
    agency = re.compile(r"STATE[\s-]*GOVT|HEALTH\s+DEPARTMENT|DEPARTMENT.*HEALTH|DEPT.*HEALTH", re.IGNORECASE)

    party_pub = [r for r in rows if r.party_name and pub.match(r.party_name)]
    party_ag = [r for r in rows if r.party_name and agency.search(r.party_name)]
    heirs_pub = [r for r in rows if r.heirs and pub.match(r.heirs)]
    heirs_ag = [r for r in rows if r.heirs and agency.search(r.heirs)]

    print(f"\nparty_name == PUBLIC placeholder : {len(party_pub)}")
    print(Counter((r.county, r.state) for r in party_pub))
    print(f"party_name is a filing agency    : {len(party_ag)}")
    print(Counter(r.party_name for r in party_ag).most_common(10))
    print(f"\nheirs == PUBLIC placeholder      : {len(heirs_pub)}")
    print(Counter((r.county, r.state) for r in heirs_pub))
    print(f"heirs is a filing agency         : {len(heirs_ag)}")
    print(Counter(r.heirs for r in heirs_ag).most_common(10))

    king = [r for r in rows if (r.county or "").lower() == "king"]
    odd = [r for r in king if r.parcel_id and len(r.parcel_id.strip()) != 10]
    print(f"\nKing probate rows: {len(king)}; parcel_id length != 10: {len(odd)}")
    print(Counter(len(r.parcel_id.strip()) for r in king))
    for r in odd[:20]:
        print(f"  {r.id} pid={r.parcel_id!r} party={r.party_name!r}")

    # Whole-table King parcel length scan (all record types)
    with system_sync_session() as db:
        allk = db.execute(
            select(func.length(Result.parcel_id), func.count())
            .join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .where(func.lower(ScraperConfig.county) == "king", Result.parcel_id.isnot(None))
            .group_by(func.length(Result.parcel_id))
        ).all()
    print("\nALL King results, parcel_id length histogram:", dict(allk))


if __name__ == "__main__":
    main()
