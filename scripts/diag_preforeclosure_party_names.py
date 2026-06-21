"""Measure pre_foreclosure party_name health per county in prod (READ-ONLY).

The user reports that most pre_foreclosure leads show no party_name (or a
placeholder). Before changing any scraper we need the ground truth: for every
county that has produced pre_foreclosure Results, how many rows have a NULL/empty
party_name, how many look like a real PERSON, and how many look like a COMPANY
(trustee/servicer) that slipped into party_name.

County comes from results → jobs → scraper_configs (Result has no county column).
Read-only, so it uses system_sync_session() and is safe to run on prod via:

    railway run --service worker python scripts/diag_preforeclosure_party_names.py

Optionally pass county slugs to restrict:
    railway run --service worker python scripts/diag_preforeclosure_party_names.py king snohomish
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.preforeclosure import is_person_name  # noqa: E402

WANT = [c.lower() for c in sys.argv[1:]]


def main() -> None:
    print("=" * 72)
    print("pre_foreclosure party_name health by county (READ-ONLY, all tenants)")
    print("=" * 72)

    with system_sync_session() as db:
        # Active connectors that advertise pre_foreclosure.
        conns = db.execute(
            text(
                "SELECT county, state, active, health_status, record_types::text "
                "FROM county_connectors "
                "WHERE record_types::text ILIKE '%pre_foreclosure%' "
                "ORDER BY state, county"
            )
        ).fetchall()
        print(f"\nConnectors advertising pre_foreclosure: {len(conns)}")
        for c in conns:
            print(f"  {c[0]:<14} {c[1]}  active={c[2]}  health={c[3]}")

        # All pre_foreclosure results, joined to county via scraper_configs.
        rows = db.execute(
            text(
                """
                SELECT sc.county AS county,
                       r.party_name AS party_name,
                       r.parcel_id AS parcel_id,
                       r.property_address AS addr
                FROM results r
                JOIN jobs j ON j.id = r.job_id
                JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                WHERE sc.record_type = 'pre_foreclosure'
                """
            )
        ).fetchall()

    print(f"\nTotal pre_foreclosure Result rows (all tenants): {len(rows)}")

    by_county: dict[str, list] = {}
    for r in rows:
        by_county.setdefault((r.county or "?").lower(), []).append(r)

    counties = sorted(by_county)
    if WANT:
        counties = [c for c in counties if c in WANT]

    for county in counties:
        recs = by_county[county]
        total = len(recs)
        empty = [x for x in recs if not (x.party_name or "").strip()]
        person = [x for x in recs if (x.party_name or "").strip() and is_person_name(x.party_name)]
        company = [
            x for x in recs
            if (x.party_name or "").strip() and not is_person_name(x.party_name)
        ]
        print("\n" + "-" * 72)
        print(f"COUNTY: {county}   total={total}")
        print(f"  empty/NULL party_name : {len(empty)}  ({100*len(empty)//total if total else 0}%)")
        print(f"  PERSON-looking        : {len(person)}  ({100*len(person)//total if total else 0}%)")
        print(f"  COMPANY-looking       : {len(company)}  ({100*len(company)//total if total else 0}%)")
        if person:
            print("  sample PERSON party_names:")
            for x in person[:8]:
                print(f"    • {x.party_name!r}")
        if company:
            print("  sample COMPANY/other party_names (suspect):")
            for x in company[:8]:
                print(f"    • {x.party_name!r}")
        if empty:
            print("  sample EMPTY rows (parcel | addr):")
            for x in empty[:5]:
                print(f"    • {x.parcel_id!r} | {x.addr!r}")


if __name__ == "__main__":
    main()
