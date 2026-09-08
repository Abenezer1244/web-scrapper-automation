"""Repair result rows that say "already delivered" with no claim behind them.

    railway run --service worker python scripts/repair_orphaned_duplicate_flags.py --dry-run
    railway run --service worker python scripts/repair_orphaned_duplicate_flags.py --apply
    ... --user <uuid>            restrict to one account
    ... --job <uuid>             restrict to hashes appearing on one job

A row with ``is_duplicate = true`` asserts the account received that lead on an
earlier run. The ``(user_id, dedup_hash)`` row in ``delivered_records`` is what
makes that true. When a claim is released -- by the plan cap, by an upload
failure, or by ``release_stranded_dedup_claims.py`` -- the rows OTHER jobs
already flagged against it keep their flag. The assertion outlives its evidence.

Production hit this on 2026-09-04: a King tax_delinquent run claimed 16,761
hashes, failed on the plan cap, and could not release (the worker role was
missing DELETE on delivered_records at the time). Two later runs saw the
stranded claims and each reported "0 new, 17,157 duplicates". The claims were
released afterwards, leaving 33,522 rows describing a delivery that never
happened, and 16,761 leads the account had scraped three times and never
received.

The repair restores a coherent state per (user, hash):

  * the EARLIEST row on a DONE job becomes the delivery: ``is_duplicate=false``,
    and a claim is written pointing at that job.
  * every later row stays a duplicate and is stamped with that job as its
    source, so the results page can finally name it.
  * rows on jobs that never finished are left alone. A failed run delivered
    nothing, so its rows are not a delivery and must not become one.

Deliberately does NOT touch ``record_count``, ``billed_count`` or
``billing_applied_at``. Those are billing-time snapshots of what was actually
charged, and this repair charges nothing. ``new_count`` on the results page is a
live count and will legitimately exceed ``record_count`` for a repaired job --
the same divergence get_results already documents for post-finalization repairs.

Idempotent: once a hash has a claim it is no longer an orphan, so a re-run is a
no-op.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

# Rows asserting a delivery with no claim behind them, ranked so that the
# earliest DONE job wins the delivery. Restricted to DONE jobs on purpose: a
# failed run delivered nothing, and promoting its rows would invent a delivery.
#
# FULLY-STATIC SQL. The optional --user / --job scopes are expressed as
# NULL-tolerant bound predicates rather than interpolated fragments, so there is
# no string building anywhere and every value arrives as a parameter -- the same
# rule tasks_helpers/enrich.py follows for its skip-trace reuse statement.
_RANKED = """
WITH orphan AS (
    SELECT DISTINCT r.user_id, r.dedup_hash
    FROM results r
    WHERE r.is_duplicate IS TRUE
      AND r.dedup_hash IS NOT NULL
      AND (CAST(:uid AS uuid) IS NULL OR r.user_id = CAST(:uid AS uuid))
      AND (CAST(:jid AS uuid) IS NULL OR r.dedup_hash IN (
              SELECT dedup_hash FROM results
              WHERE job_id = CAST(:jid AS uuid) AND dedup_hash IS NOT NULL))
      AND NOT EXISTS (
          SELECT 1 FROM delivered_records dr
          WHERE dr.user_id = r.user_id AND dr.dedup_hash = r.dedup_hash
      )
),
ranked AS (
    SELECT r.id, r.job_id, r.user_id, r.dedup_hash,
           r.parcel_id, r.property_address, j.created_at AS job_created_at,
           row_number() OVER (
               PARTITION BY r.user_id, r.dedup_hash
               ORDER BY j.created_at, r.id
           ) AS rn
    FROM results r
    JOIN orphan o ON o.user_id = r.user_id AND o.dedup_hash = r.dedup_hash
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id
    WHERE j.status = 'done'
)
"""

# Composed once, at import, from the literal above. Kept as named constants so
# no query is built at the call site: every text() below receives a plain name.
_Q_SUMMARY = _RANKED + """
SELECT count(*) FILTER (WHERE rn = 1) AS winners,
       count(*) FILTER (WHERE rn > 1) AS losers,
       count(DISTINCT user_id) AS users,
       count(DISTINCT job_id) AS jobs
FROM ranked
"""

_Q_PER_JOB = _RANKED + """
SELECT job_id, user_id,
       count(*) FILTER (WHERE rn = 1) AS becomes_new,
       count(*) FILTER (WHERE rn > 1) AS stays_duplicate
FROM ranked GROUP BY job_id, user_id ORDER BY becomes_new DESC
"""

_Q_CLAIM = _RANKED + """
INSERT INTO delivered_records
    (id, user_id, dedup_hash, first_result_id, first_job_id,
     parcel_id, property_address, first_delivered_at)
SELECT gen_random_uuid(), user_id, dedup_hash, id, job_id,
       parcel_id, property_address, job_created_at
FROM ranked WHERE rn = 1
ON CONFLICT (user_id, dedup_hash) DO NOTHING
RETURNING first_result_id
"""

_Q_REMAINING = _RANKED + """
SELECT count(*) FROM ranked
"""

_Q_PROMOTE = """
UPDATE results SET is_duplicate = false,
                   duplicate_source_job_id = NULL,
                   duplicate_source_at = NULL,
                   duplicate_reason = NULL
WHERE id = ANY(CAST(:ids AS uuid[]))
"""

_Q_STAMP = """
UPDATE results r
SET duplicate_source_job_id = dr.first_job_id,
    duplicate_source_at = dr.first_delivered_at,
    duplicate_reason = 'prior_run'
FROM delivered_records dr
WHERE dr.user_id = r.user_id
  AND dr.dedup_hash = r.dedup_hash
  AND r.is_duplicate IS TRUE
  AND r.duplicate_reason IS NULL
  AND dr.first_result_id = ANY(CAST(:ids AS uuid[]))
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user")
    ap.add_argument("--job")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        print("pass --dry-run or --apply")
        sys.exit(2)

    p = {"uid": args.user, "jid": args.job}

    with system_sync_session() as db:
        summary = db.execute(
            text(_Q_SUMMARY),
            p,
        ).first()
        print(f"leads to restore as delivered: {summary.winners}")
        print(f"rows that stay duplicate but gain a source: {summary.losers}")
        print(f"across {summary.users} user(s) / {summary.jobs} job(s)")

        print("\nper job:")
        for row in db.execute(
            text(_Q_PER_JOB),
            p,
        ).fetchall():
            meta = db.execute(
                text("SELECT record_count, billed_count FROM jobs WHERE id = :j"),
                {"j": str(row.job_id)},
            ).first()
            print(f"  job={row.job_id} user={row.user_id}")
            print(f"     becomes deliverable: {row.becomes_new}  "
                  f"stays duplicate: {row.stays_duplicate}")
            print(f"     record_count={meta.record_count} billed_count={meta.billed_count} "
                  f"(NOT changed by this repair)")

        if not args.apply:
            print("\nDRY RUN - nothing written")
            return

        # 1. Claim the winners. ON CONFLICT DO NOTHING so a concurrent delivery
        #    that legitimately took the hash first keeps it, and step 2 below
        #    only promotes rows this statement actually claimed.
        claimed = db.execute(
            text(_Q_CLAIM),
            p,
        ).fetchall()
        won = [str(r.first_result_id) for r in claimed]
        print(f"\nclaims written: {len(won)}")

        # 2. Promote exactly the rows whose claim this run took.
        promoted = db.execute(
            text(_Q_PROMOTE),
            {"ids": won},
        ).rowcount
        print(f"rows restored as delivered: {promoted}")

        # 3. Point every remaining duplicate at the run that now holds the claim,
        #    so the results page can name it instead of saying "an earlier run".
        stamped = db.execute(
            text(_Q_STAMP),
            {"ids": won},
        ).rowcount
        print(f"duplicates given a source run: {stamped}")

        db.commit()

        left = db.execute(
            text(_Q_REMAINING), p
        ).scalar()
        print(f"\norphaned rows remaining: {left}")
        if left:
            print("WARNING: did not converge - re-run the dry run and compare")


if __name__ == "__main__":
    main()
