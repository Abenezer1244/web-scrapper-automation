"""Re-match existing pre_foreclosure leads against the NTS cache, over a wide window.

WHY THIS EXISTS
    The King PIN/account bridge (2026-09-20) unlocked notice/lead pairs the matcher had
    been vetoing as parcel conflicts. Measured in prod 2026-09-19: 14 of 38 King notices
    carry the 12-digit tax account number while the recorder index emits the 10-digit
    PIN, and 7 pairs were being dropped, two of them live upcoming auctions.

    The daily beat (src/workers/nts_matcher_task.match_nts_notices) already re-matches
    every unmatched lead created in the last _RECENT_DAYS (180) and is idempotent, so
    for anything inside that window the beat IS the backfill and this script is only a
    way to run it NOW and see what it would do first. This exists for two things the
    beat cannot do: reach leads OLDER than 180 days, and show a reviewable dry run
    before anything is written.

WHAT IT TOUCHES, AND WHAT IT CANNOT
    It calls the same _match_and_write the beat calls. That writes exactly five things
    on a Result: auction_date, default_amount, nts_match_confidence, nts_notice_id and a
    merged enrichment_data. It does NOT create leads, does NOT read or write users.
    records_used / quota, does NOT touch delivered_records or any delivery state, does
    NOT enqueue skip tracing, does NOT change user_id, and does NOT create jobs. A
    re-run is a no-op: _write_match claims only rows that are unset or hold a PAST
    auction, so a lead that already carries a future sale updates 0 rows.

    Because it only ever fills a blank (or replaces an already-past sale with a live
    one), an ALREADY DELIVERED lead is corrected in place rather than duplicated - the
    canonical row is the one the customer's results view and CSV both read.

DRY RUN IS THE DEFAULT
    Without --apply the work happens inside a transaction that is rolled back, so the
    printed counts are what WOULD change, measured against real data rather than
    estimated.

Usage:
    railway run --service worker python scripts/backfill_nts_matches.py
    railway run --service worker python scripts/backfill_nts_matches.py --county king
    railway run --service worker python scripts/backfill_nts_matches.py --county king --apply
    # reach leads older than the beat's 180-day window
    ... --county king --days 400 --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--county", default=None,
                    help="one county slug; default = every county with an NTS source")
    ap.add_argument("--days", type=int, default=180,
                    help="how far back to consider leads, by created_at (default 180, "
                         "the beat's own window)")
    ap.add_argument("--apply", action="store_true",
                    help="commit the writes (default: dry run, rolled back)")
    args = ap.parse_args()

    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _sa_text

    from src.db.session import system_sync_session
    from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES, _match_and_write

    counties = [args.county.lower()] if args.county else list(NTS_MATCH_COUNTIES)
    unknown = [c for c in counties if c not in NTS_MATCH_COUNTIES]
    if unknown:
        print(f"ERROR: no NTS source wired for {unknown}. "
              f"Known: {list(NTS_MATCH_COUNTIES)}")
        return 2

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    today = datetime.now(UTC).date()
    mode = "APPLY" if args.apply else "DRY RUN (rolled back)"
    print(f"=== NTS re-match backfill - {mode} ===")
    print(f"counties={counties} window={args.days}d (created_at >= {cutoff.date()})\n")

    total_candidates = total_matched = 0
    for county in counties:
        with system_sync_session() as db:
            rows = db.execute(
                _sa_text(
                    """
                    SELECT r.id, r.parcel_id, r.property_address, r.party_name,
                           r.date_recorded_parsed
                    FROM results r JOIN jobs j ON j.id = r.job_id
                    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                    WHERE sc.record_type = 'pre_foreclosure'
                      AND lower(sc.county) = :county
                      AND (r.auction_date IS NULL OR r.auction_date < :today)
                      AND r.created_at >= :cutoff
                    """
                ),
                {"county": county, "cutoff": cutoff, "today": today},
            ).fetchall()
            candidates = [dict(r._mapping) for r in rows]
            before = _fill_counts(db, county)

            # commit=False either way: we control the transaction so a dry run can be
            # rolled back after MEASURING the real effect, not a guess at it.
            matched = _match_and_write(db, candidates, county=county, commit=False)
            after = _fill_counts(db, county)

            if args.apply:
                db.commit()
            else:
                db.rollback()

            total_candidates += len(candidates)
            total_matched += matched
            print(f"  {county:<10} candidates={len(candidates):<5} newly_matched={matched}")
            print(f"  {'':<10} auction_date filled {before['auction']} -> {after['auction']}"
                  f"   default_amount {before['amount']} -> {after['amount']}")

    print(f"\ntotal: {total_matched} leads enriched from {total_candidates} candidates")
    if not args.apply:
        print("DRY RUN - nothing was written. Re-run with --apply to commit.")
    return 0


def _fill_counts(db, county: str) -> dict:
    """Current auction_date / default_amount fill counts for one county's leads."""
    from sqlalchemy import text as _sa_text
    row = db.execute(
        _sa_text(
            """
            SELECT count(r.auction_date) AS auction, count(r.default_amount) AS amount
            FROM results r JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE sc.record_type = 'pre_foreclosure' AND lower(sc.county) = :county
            """
        ),
        {"county": county},
    ).first()
    return {"auction": row.auction, "amount": row.amount}


if __name__ == "__main__":
    raise SystemExit(main())
