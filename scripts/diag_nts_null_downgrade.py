"""Read-only: how often does the cache hold NULL for a field a delivered lead still carries?

`_upsert_notice` refreshes EVERY mutable field ON CONFLICT (source, ts_number), so a
re-crawl that parses the same notice but fails to extract a field overwrites a good value
with NULL. Leads copied the value at match time and keep it, so the divergence is visible.

    railway run --service worker python scripts/diag_nts_null_downgrade.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import text as t

    from src.db.session import system_sync_session

    with system_sync_session() as db:
        print("=== notices with a NULL in a field the matcher copies ===")
        for r in db.execute(t(
            """
            SELECT source, count(*) n,
                   count(*) FILTER (WHERE principal_owing IS NULL) null_owing,
                   count(*) FILTER (WHERE auction_date IS NULL) null_auction,
                   count(*) FILTER (WHERE parcel IS NULL OR parcel = '') null_parcel,
                   count(*) FILTER (WHERE property_address_normalized IS NULL
                                    OR property_address_normalized = '') null_addr,
                   count(*) FILTER (WHERE grantor IS NULL OR grantor = '') null_grantor,
                   count(*) FILTER (WHERE trustee IS NULL OR trustee = '') null_trustee
            FROM nts_notices GROUP BY 1 ORDER BY 1
            """)).fetchall():
            print("  ", dict(r._mapping))

        print("\n=== leads holding a value their notice has since lost ===")
        rows = [dict(r._mapping) for r in db.execute(t(
            """
            SELECT sc.name job, lower(sc.county) county, r.party_name, r.parcel_id,
                   r.auction_date, r.default_amount,
                   n.source, n.ts_number, n.auction_date n_auction,
                   n.principal_owing n_owing, n.fetched_at
            FROM results r
            JOIN nts_notices n ON n.id = r.nts_notice_id
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE (r.default_amount IS NOT NULL AND n.principal_owing IS NULL)
               OR (r.auction_date IS NOT NULL AND n.auction_date IS NULL)
            ORDER BY n.source, r.party_name
            """)).fetchall()]
        print(f"  count={len(rows)}")
        for m in rows:
            print(f"   {m['source']} ts={m['ts_number']!r} {m['party_name'][:28]!r}")
            print(f"      lead   auction={m['auction_date']} owed={m['default_amount']}")
            print(f"      notice auction={m['n_auction']} owed={m['n_owing']} "
                  f"fetched={m['fetched_at']}")

        print("\n=== active future notices missing an amount (the resweep's queue) ===")
        for r in db.execute(t(
            """
            SELECT source, count(*) n
            FROM nts_notices
            WHERE is_active AND auction_date >= current_date AND principal_owing IS NULL
            GROUP BY 1 ORDER BY 1
            """)).fetchall():
            print("  ", dict(r._mapping))
        print("  (none listed = every live notice carries an amount)")


if __name__ == "__main__":
    main()
