"""Backfill results.duplicate_source_* for rows classified before migration 089.

    railway run --service worker python scripts/backfill_duplicate_provenance.py --dry-run
    railway run --service worker python scripts/backfill_duplicate_provenance.py --apply

Migration 089 stamps provenance at classification time. Rows flagged before it
carry NULL, and the results page reports them as unattributed. Where the claim
ledger STILL resolves the answer, this recovers it so those pages can name the
run instead of staying vague.

It cannot recover everything, and deliberately does not try:

  * 82% of production claims point at a purged jobs row. Those stay NULL.
  * A claim released and re-claimed by a LATER job now names the wrong run.
    Guarded on the CLAIM time, not the job's creation time (Codex): a job
    created earlier can still execute, retry, or re-claim a released hash AFTER
    the viewed run finished, and comparing created_at alone would stamp that as
    the original source forever. first_delivered_at is when the claim was
    actually made, so requiring it to precede the viewed run is the real
    evidence. created_at is kept as well because both must hold.
  * A claim held by a job that never reached 'done' is not evidence of delivery.
    Guarded by requiring status='done'.

Anything failing a guard is left NULL. Unattributed is a correct answer; a
confident wrong one is what caused the incident this whole change came from.

Idempotent: only touches rows where duplicate_reason IS NULL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

# One statement, so the read and the write cannot disagree. Every guard is a
# JOIN predicate rather than a post-filter, so a row either gets a provenance
# that satisfies all of them or keeps NULL.
BACKFILL = """
UPDATE results r
SET duplicate_reason = 'prior_run',
    duplicate_source_job_id = dr.first_job_id,
    duplicate_source_at = dr.first_delivered_at
FROM results src
JOIN jobs own      ON own.id = src.job_id
JOIN delivered_records dr
                   ON dr.user_id = src.user_id
                  AND dr.dedup_hash = src.dedup_hash
JOIN jobs source   ON source.id = dr.first_job_id
                  AND source.user_id = src.user_id
                  AND source.status = 'done'
                  AND source.created_at < own.created_at
                  AND dr.first_delivered_at < own.created_at
WHERE r.id = src.id
  AND src.is_duplicate IS TRUE
  AND src.dedup_hash IS NOT NULL
  AND src.duplicate_reason IS NULL
"""

PREVIEW = """
SELECT count(*) n, count(DISTINCT src.job_id) jobs, count(DISTINCT src.user_id) users
FROM results src
JOIN jobs own      ON own.id = src.job_id
JOIN delivered_records dr
                   ON dr.user_id = src.user_id
                  AND dr.dedup_hash = src.dedup_hash
JOIN jobs source   ON source.id = dr.first_job_id
                  AND source.user_id = src.user_id
                  AND source.status = 'done'
                  AND source.created_at < own.created_at
                  AND dr.first_delivered_at < own.created_at
WHERE src.is_duplicate IS TRUE
  AND src.dedup_hash IS NOT NULL
  AND src.duplicate_reason IS NULL
"""

REMAINING = """
SELECT count(*) FROM results
WHERE is_duplicate IS TRUE AND dedup_hash IS NOT NULL AND duplicate_reason IS NULL
"""


def main() -> None:
    apply = "--apply" in sys.argv
    if not apply and "--dry-run" not in sys.argv:
        print("pass --dry-run or --apply")
        sys.exit(2)

    with system_sync_session() as db:
        before = db.execute(text(REMAINING)).scalar()
        p = db.execute(text(PREVIEW)).first()
        print(f"unattributed duplicate rows before: {before}")
        print(f"recoverable: {p.n} rows across {p.jobs} jobs / {p.users} users")
        print(f"will stay unattributed: {before - p.n}")

        if not apply:
            print("\nDRY RUN — nothing written")
            return

        res = db.execute(text(BACKFILL))
        db.commit()
        after = db.execute(text(REMAINING)).scalar()
        print(f"\nstamped {res.rowcount} rows")
        print(f"unattributed duplicate rows after: {after}")
        if after != before - p.n:
            print("WARNING: convergence mismatch — re-run the dry run and compare")


if __name__ == "__main__":
    main()
