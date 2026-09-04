"""Audit: does every matched lead's nts_notice_id still resolve to a notice for the
SAME property?

`nts_notices` is upserted ON CONFLICT (source, ts_number), so the moment a backfill or
re-crawl inserts the real notice for a key that a mis-bound row was squatting on, that
row's CONTENT is replaced wholesale — and any lead pointing at it is left aimed at a
stranger's parcel. The lead's own auction_date/default_amount are unaffected (copied at
match time), but the audit pointer is wrong. Found 1 of 4 King leads this way on
2026-09-04, right after running the archive backfill.

Fix what this reports with:
    scripts/repair_nts_ts_number.py --source <source> --results --apply

    railway run --service worker python scripts/diag_nts_pointer_integrity.py [--source S]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="queen_anne_news")
    args = ap.parse_args()
    src = args.source

    from sqlalchemy import text as t

    from src.db.session import system_sync_session
    from src.scrapers.sources.nts_matcher import _norm_parcel

    with system_sync_session() as db:
        rows = [dict(r._mapping) for r in db.execute(t(
            """
            SELECT sc.name AS job_name, lower(sc.county) county, sc.record_type,
                   r.party_name, r.parcel_id, r.auction_date, r.default_amount,
                   r.enrichment_data -> 'nts' ->> 'ts_number' AS stored_ts,
                   n.ts_number AS notice_ts, n.parcel AS notice_parcel,
                   n.auction_date AS notice_auction, n.principal_owing,
                   n.is_active, n.source_url
            FROM results r
            JOIN nts_notices n ON n.id = r.nts_notice_id
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE n.source = :src
            ORDER BY r.created_at
            """), {"src": src}).fetchall()]

    print(f"leads pointing at a {src} notice: {len(rows)}\n")
    bad = 0
    for m in rows:
        same = _norm_parcel(m["parcel_id"]) == _norm_parcel(m["notice_parcel"])
        if not same:
            bad += 1
        print(f"{'OK ' if same else 'MISMATCH'} {m['job_name']!r} {m['party_name']!r}")
        print(f"    lead   parcel={m['parcel_id']} auction={m['auction_date']} "
              f"owed={m['default_amount']} stored_ts={m['stored_ts']!r}")
        print(f"    notice parcel={m['notice_parcel']} auction={m['notice_auction']} "
              f"owed={m['principal_owing']} ts={m['notice_ts']!r} active={m['is_active']}")
        print(f"    issue={m['source_url'].rsplit('/', 1)[-1]}")
    print(f"\n>>> pointers aimed at a DIFFERENT property: {bad}/{len(rows)}")

    print("\n=== does a correctly-keyed notice exist for each lead's parcel? ===")
    with system_sync_session() as db:
        for m in rows:
            hit = db.execute(t(
                """
                SELECT ts_number, auction_date, principal_owing, is_active
                FROM nts_notices
                WHERE source = :src
                  AND upper(regexp_replace(coalesce(parcel,''),'[^A-Za-z0-9]','','g'))
                      = upper(regexp_replace(:p,'[^A-Za-z0-9]','','g'))
                ORDER BY auction_date DESC
                """), {"p": m["parcel_id"], "src": src}).fetchall()
            print(f"  {m['party_name']!r} parcel={m['parcel_id']}: {len(hit)} notice(s)")
            for h in hit:
                print("     ", dict(h._mapping))

    print("\n=== King cache totals now ===")
    with system_sync_session() as db:
        r = db.execute(t(
            "SELECT count(*) n, count(*) FILTER (WHERE is_active) active, "
            "count(*) FILTER (WHERE is_active AND auction_date >= current_date) live "
            "FROM nts_notices WHERE source = :src"), {"src": src}).fetchone()
        print("  ", dict(r._mapping))


if __name__ == "__main__":
    main()
