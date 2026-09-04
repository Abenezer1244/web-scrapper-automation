"""Read-only: audit the leads enriched by the most recent matcher run (matched_at today).

    railway run --service worker python scripts/diag_test8_new_matches.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sqlalchemy import text as t

    from src.db.session import system_sync_session
    from src.scrapers.sources.nts_matcher import _norm_parcel

    with system_sync_session() as db:
        rows = [dict(r._mapping) for r in db.execute(t(
            """
            SELECT sc.name job, lower(sc.county) county, sc.record_type,
                   r.party_name, r.parcel_id, r.property_address,
                   r.auction_date, r.default_amount, r.nts_match_confidence,
                   r.enrichment_data -> 'nts' ->> 'matched_at' AS matched_at,
                   n.parcel n_parcel, n.auction_date n_auction, n.principal_owing,
                   n.grantor, n.source, n.is_active
            FROM results r
            JOIN nts_notices n ON n.id = r.nts_notice_id
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE r.enrichment_data -> 'nts' ->> 'matched_at' >= :today
            ORDER BY sc.name, r.party_name
            """), {"today": "2026-09-04"}).fetchall()]

        print(f"leads matched today: {len(rows)}\n")
        future = pastn = 0
        for m in rows:
            ok_parcel = _norm_parcel(m["parcel_id"]) == _norm_parcel(m["n_parcel"])
            ok_date = m["auction_date"] == m["n_auction"]
            ok_amt = str(m["default_amount"]) == str(m["principal_owing"])
            tag = "LIVE" if m["is_active"] else "PAST"
            if m["is_active"]:
                future += 1
            else:
                pastn += 1
            flag = "" if (ok_parcel and ok_date and ok_amt) else "  <<< CHECK"
            print(f"  [{tag}] {m['county']}/{m['record_type']} {m['job']!r}")
            print(f"        {m['party_name']!r} parcel={m['parcel_id']}")
            print(f"        auction={m['auction_date']} owed={m['default_amount']} "
                  f"conf={m['nts_match_confidence']}{flag}")
            print(f"        notice parcel={m['n_parcel']} grantor={str(m['grantor'])[:40]!r}")
        print(f"\n  live-auction matches={future}  past-auction matches={pastn}")

        print("\n=== oldest past auction attached to ANY lead (bound is 180d) ===")
        r = db.execute(t(
            "SELECT min(auction_date) oldest, current_date - min(auction_date) age_days "
            "FROM results WHERE auction_date IS NOT NULL")).fetchone()
        print("  ", dict(r._mapping))


if __name__ == "__main__":
    main()
