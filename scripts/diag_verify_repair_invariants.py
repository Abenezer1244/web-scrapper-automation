"""READ-ONLY: prove the orphaned-flag repair did not corrupt anything.

    railway run --service worker python scripts/diag_verify_repair_invariants.py

Every check is written so that a NON-ZERO count means the repair (or something
else) left the data in a state the product cannot honestly describe. Ids and
counts only; no PII.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

CHECKS = [
    ("claims whose first_result_id row no longer exists", """
        SELECT count(*) FROM delivered_records dr
        LEFT JOIN results r ON r.id = dr.first_result_id
        WHERE dr.first_result_id IS NOT NULL AND r.id IS NULL
    """),
    ("claims whose first_result_id belongs to a DIFFERENT user (tenant leak)", """
        SELECT count(*) FROM delivered_records dr
        JOIN results r ON r.id = dr.first_result_id
        WHERE r.user_id <> dr.user_id
    """),
    ("claims whose first_job_id belongs to a DIFFERENT user (tenant leak)", """
        SELECT count(*) FROM delivered_records dr
        JOIN jobs j ON j.id = dr.first_job_id
        WHERE j.user_id <> dr.user_id
    """),
    ("claims whose dedup_hash disagrees with the row they name", """
        SELECT count(*) FROM delivered_records dr
        JOIN results r ON r.id = dr.first_result_id
        WHERE r.dedup_hash IS DISTINCT FROM dr.dedup_hash
    """),
    ("claims naming a row that is itself flagged is_duplicate (contradiction)", """
        SELECT count(*) FROM delivered_records dr
        JOIN results r ON r.id = dr.first_result_id
        WHERE r.is_duplicate IS TRUE
    """),
    ("claims naming a job that never reached done", """
        SELECT count(*) FROM delivered_records dr
        JOIN jobs j ON j.id = dr.first_job_id
        WHERE j.status <> 'done'
    """),
    ("duplicate rows with NO claim behind them (the bug being repaired)", """
        SELECT count(*) FROM results r
        WHERE r.is_duplicate IS TRUE AND r.dedup_hash IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM delivered_records dr
                          WHERE dr.user_id = r.user_id AND dr.dedup_hash = r.dedup_hash)
    """),
    ("delivered rows with NO claim (would be delivered+billed AGAIN)", """
        SELECT count(*) FROM results r
        WHERE r.is_duplicate IS FALSE AND r.dedup_hash IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM delivered_records dr
                          WHERE dr.user_id = r.user_id AND dr.dedup_hash = r.dedup_hash)
    """),
    ("stamped provenance pointing at ANOTHER user's job (tenant leak)", """
        SELECT count(*) FROM results r
        JOIN jobs j ON j.id = r.duplicate_source_job_id
        WHERE r.duplicate_source_job_id IS NOT NULL AND j.user_id <> r.user_id
    """),
    ("provenance pointing FORWARD: source job newer than the job showing it", """
        SELECT count(*) FROM results r
        JOIN jobs own ON own.id = r.job_id
        JOIN jobs src ON src.id = r.duplicate_source_job_id
        WHERE r.duplicate_reason = 'prior_run' AND src.created_at > own.created_at
    """),
    ("rows stamped prior_run that point at their OWN job", """
        SELECT count(*) FROM results r
        WHERE r.duplicate_reason = 'prior_run' AND r.duplicate_source_job_id = r.job_id
    """),
    ("rows with provenance but not flagged duplicate", """
        SELECT count(*) FROM results r
        WHERE r.duplicate_reason IS NOT NULL AND r.is_duplicate IS FALSE
    """),
    ("duplicate_reason values outside the known set", """
        SELECT count(*) FROM results
        WHERE duplicate_reason IS NOT NULL
          AND duplicate_reason NOT IN ('prior_run', 'same_run', 'superseded')
    """),
]


def main() -> None:
    with system_sync_session() as db:
        bad = 0
        for label, sql in CHECKS:
            n = db.execute(text(sql)).scalar()
            mark = "OK  " if n == 0 else "FAIL"
            if n:
                bad += 1
            print(f"  [{mark}] {n:>7}  {label}")

        print("\n=== billing untouched for the repaired account ===")
        u = db.execute(text("""
            SELECT records_used, records_limit, plan FROM users
            WHERE id = CAST('01dc9396-9a36-49b5-9b98-5343ec107232' AS uuid)
        """)).first()
        print(f"  records_used={u.records_used}/{u.records_limit} plan={u.plan} "
              "(was 1001/1000 before the repair)")
        for j in db.execute(text("""
            SELECT id, status, record_count, billed_count, billing_applied_at
            FROM jobs WHERE id IN (
                CAST('68d83263-b27d-4282-95c1-5abeb7b8d924' AS uuid),
                CAST('035501e3-14d4-48d1-9799-8b93737f5f85' AS uuid),
                CAST('60a0e80c-25b2-4be2-bfa1-b6258e31f9d1' AS uuid))
            ORDER BY created_at
        """)).fetchall():
            print(f"  job={str(j.id)[:8]} {j.status:7} record_count={j.record_count} "
                  f"billed={j.billed_count} applied={j.billing_applied_at}")

        print(f"\n{'ALL INVARIANTS HOLD' if bad == 0 else str(bad) + ' INVARIANT(S) VIOLATED'}")


if __name__ == "__main__":
    main()
