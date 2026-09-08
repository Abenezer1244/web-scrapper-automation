"""One-off: correct record_count on the job the 2026-09-08 repair restored.

The repair ran before it knew record_count was the DELIVERY headline rather than
a billing record, so it left the jobs list saying 0 for a run whose results page
now lists thousands. Same predicate the repair script now uses.

    railway run --service worker python scripts/fix_repaired_job_headline.py --dry-run
    railway run --service worker python scripts/fix_repaired_job_headline.py --apply

Does NOT touch billed_count, billing_applied_at, or users.records_used.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

JOB = "68d83263-b27d-4282-95c1-5abeb7b8d924"

LIVE = """
SELECT count(*) FROM results r
WHERE r.job_id = CAST(:j AS uuid)
  AND r.is_duplicate IS FALSE
  AND COALESCE(r.enrichment_data->>'delivery_excluded_reason', '') <> 'over_quota'
  AND (COALESCE(btrim(r.property_address), '') NOT IN ('', '(enrichment unavailable)')
       OR COALESCE(btrim(r.mailing_address), '') NOT IN ('', '(enrichment unavailable)'))
"""

with system_sync_session() as db:
    apply = "--apply" in sys.argv
    if not apply and "--dry-run" not in sys.argv:
        print("pass --dry-run or --apply")
        sys.exit(2)
    before = db.execute(text(
        "SELECT record_count, billed_count, billing_applied_at FROM jobs "
        "WHERE id = CAST(:j AS uuid)"), {"j": JOB}).first()
    live = db.execute(text(LIVE), {"j": JOB}).scalar()
    print(f"job {JOB[:8]}")
    print(f"  record_count now: {before.record_count}   live deliverable: {live}")
    print(f"  billed_count: {before.billed_count} (unchanged) "
          f"billing_applied_at: {before.billing_applied_at} (unchanged)")
    if not apply:
        print("\nDRY RUN - nothing written")
    else:
        n = db.execute(text(
            "UPDATE jobs SET record_count = :n WHERE id = CAST(:j AS uuid) "
            "AND record_count <> :n"), {"n": live, "j": JOB}).rowcount
        db.commit()
        after = db.execute(text(
            "SELECT record_count, billed_count FROM jobs WHERE id = CAST(:j AS uuid)"),
            {"j": JOB}).first()
        print(f"\nupdated {n} job(s); record_count={after.record_count} "
              f"billed_count={after.billed_count}")
