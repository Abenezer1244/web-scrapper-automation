"""READ-ONLY diagnostic: has the same-run collapse fired yet, and does the
delivered_records claim anchor ever disagree with the collapse survivor?

The claim's first_result_id is chosen by whichever row PostgreSQL's multi-row
ON CONFLICT hit first; collapse_same_run_siblings picks its survivor by an
actionability/completeness ranking. Nothing coordinates the two, so the claim
can name a row the collapse then flags is_duplicate -- which invariant #5
("claims naming a row that is itself flagged is_duplicate") forbids.

Prints counts only. Writes nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402


QUERIES = [
    ("same_run rows in results (has the collapse fired at all?)",
     "SELECT count(*) FROM results WHERE duplicate_reason = 'same_run'"),
    ("distinct jobs that have same_run rows",
     "SELECT count(DISTINCT job_id) FROM results WHERE duplicate_reason = 'same_run'"),
    ("same_run rows whose id IS the claim anchor (invariant #5 violation)",
     "SELECT count(*) FROM results r "
     "JOIN delivered_records dr ON dr.first_result_id = r.id AND dr.user_id = r.user_id "
     "WHERE r.duplicate_reason = 'same_run'"),
    ("ALL duplicate rows that are a claim anchor (any reason)",
     "SELECT count(*) FROM results r "
     "JOIN delivered_records dr ON dr.first_result_id = r.id AND dr.user_id = r.user_id "
     "WHERE r.is_duplicate = true"),
    ("same_run groups where the surviving sibling is NOT the claim anchor",
     "SELECT count(*) FROM ("
     "  SELECT r.user_id, r.dedup_hash FROM results r "
     "  WHERE r.duplicate_reason = 'same_run' AND r.dedup_hash IS NOT NULL "
     "  GROUP BY r.user_id, r.dedup_hash"
     ") g JOIN delivered_records dr "
     "  ON dr.user_id = g.user_id AND dr.dedup_hash = g.dedup_hash "
     "JOIN results anchor ON anchor.id = dr.first_result_id "
     "WHERE anchor.is_duplicate = true"),
    ("job status values in use (batch child eligibility, P2 #3)",
     "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY 2 DESC"),
    ("batch child statuses actually seen",
     "SELECT j.status, count(*) FROM jobs j "
     "WHERE j.batch_run_id IS NOT NULL GROUP BY j.status ORDER BY 2 DESC"),
]


def main() -> None:
    with system_sync_session() as db:
        for label, sql in QUERIES:
            print(f"\n--- {label}")
            try:
                for row in db.execute(text(sql)).fetchall():
                    print("   ", tuple(row))
            except Exception as exc:
                print("    ERROR:", str(exc)[:200])
                db.rollback()


if __name__ == "__main__":
    main()
