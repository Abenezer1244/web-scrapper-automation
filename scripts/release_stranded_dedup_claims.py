"""Release cross-job dedup claims stranded by a job that never delivered them.

WHY THIS EXISTS
    `delivered_records` is the cross-job dedup ledger: one row per
    (user_id, dedup_hash) claiming "this lead has already been delivered". When a
    job fails or has rows excluded by the plan cap, tasks.py releases the claims
    it did not deliver, so those leads stay reachable on a later run.

    Production was missing DELETE on delivered_records for the worker role (a
    one-line drift between provision_rls_roles.sql and
    _cutover_step2_grants_policies.py — see PR #218). All five release paths
    raised InsufficientPrivilege and were swallowed, so claims from
    never-delivered, never-billed jobs were left behind. Those leads are then
    excluded as duplicates from every future run's results and downloads —
    silently, and forever.

    This releases the claims the worker could not. It is the SAME tenant-scoped
    predicate tasks.py uses, nothing broader.

SAFETY
    Dry-run by default; --apply required. Refuses a job that is not terminal
    (a running job may still legitimately deliver those leads), refuses if the
    row count does not match --expect, and refuses if any claim looks like it
    protects a lead an EARLIER job actually delivered. Writes a JSONL backup of
    every row before deleting.

USAGE
    railway run python scripts/release_stranded_dedup_claims.py --job <uuid>
    railway run python scripts/release_stranded_dedup_claims.py --job <uuid> --expect 16761 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_TERMINAL = ("failed", "cancelled")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="job_id whose claims to release")
    ap.add_argument("--expect", type=int, default=None, help="required row count (guard)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir", default=".")
    args = ap.parse_args()

    with system_sync_session() as db:
        job = db.execute(
            text("SELECT id::text id, user_id::text uid, status, record_count, billed_count, "
                 "billing_applied_at FROM jobs WHERE id = :j"),
            {"j": args.job},
        ).first()
        if job is None:
            raise SystemExit(f"job {args.job} not found")
        print(f"job {job.id}  status={job.status}  record_count={job.record_count}  "
              f"billed_count={job.billed_count}  billing_applied_at={job.billing_applied_at}")

        # A running job may still deliver these leads legitimately; releasing its
        # claims underneath it would let the same run re-claim or double-deliver.
        if job.status not in _TERMINAL:
            raise SystemExit(
                f"refusing: job status is '{job.status}', not one of {_TERMINAL}. "
                "Cancel it first if it is stuck."
            )

        rows = db.execute(
            text("SELECT id::text id, dedup_hash, first_result_id::text rid, "
                 "parcel_id, property_address, first_delivered_at "
                 "FROM delivered_records WHERE first_job_id = :j AND user_id = CAST(:u AS uuid)"),
            {"j": job.id, "u": job.uid},
        ).all()
        print(f"stranded claims for this job: {len(rows)}")
        if args.expect is not None and len(rows) != args.expect:
            raise SystemExit(f"refusing: expected {args.expect} rows, found {len(rows)}")
        if not rows:
            print("nothing to release")
            return

        # Guard: would releasing a claim un-protect a lead an EARLIER job really
        # delivered? (user_id, dedup_hash) is unique, so this job holding the claim
        # means it won it — but if an earlier delivered row shares the hash, the
        # claim is doing real work and must be repointed, not dropped.
        overlap = db.execute(
            text("""
              SELECT count(DISTINCT r.dedup_hash)
              FROM results r JOIN jobs j2 ON j2.id = r.job_id
              JOIN delivered_records dr ON dr.dedup_hash = r.dedup_hash
                                       AND dr.user_id = r.user_id
              WHERE dr.first_job_id = :j AND dr.user_id = CAST(:u AS uuid)
                AND r.job_id <> :j AND r.is_duplicate = false
                AND j2.status = 'done'
            """),
            {"j": job.id, "u": job.uid},
        ).scalar()
        print(f"claims also backing an earlier DELIVERED result: {overlap}")
        if overlap:
            raise SystemExit(
                f"refusing: {overlap} claim(s) protect leads an earlier completed job "
                "delivered. Those must be repointed to that job, not released."
            )

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = os.path.join(args.backup_dir, f"stranded_claims_{job.id[:8]}_{stamp}.jsonl")
        with open(backup, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "id": r.id, "dedup_hash": r.dedup_hash, "first_result_id": r.rid,
                    "first_job_id": job.id, "user_id": job.uid,
                    "parcel_id": r.parcel_id, "property_address": r.property_address,
                    "first_delivered_at": str(r.first_delivered_at),
                }) + "\n")
        print(f"backup written: {backup} ({len(rows)} rows)")

        if not args.apply:
            print("\nDRY RUN — nothing deleted. Re-run with --apply to release.")
            return

        deleted = db.execute(
            text("DELETE FROM delivered_records "
                 "WHERE first_job_id = :j AND user_id = CAST(:u AS uuid)"),
            {"j": job.id, "u": job.uid},
        ).rowcount
        left = db.execute(
            text("SELECT count(*) FROM delivered_records "
                 "WHERE first_job_id = :j AND user_id = CAST(:u AS uuid)"),
            {"j": job.id, "u": job.uid},
        ).scalar()
        if left:
            db.rollback()
            raise SystemExit(f"post-check failed: {left} rows remain — rolled back")
        db.commit()
        print(f"released {deleted} claims; 0 remain. Those leads are reachable again.")


if __name__ == "__main__":
    main()
