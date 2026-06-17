"""Fail-clean orphaned `pending` jobs (Phase 1 cleanup — guarded write).

Context: the single-job create path enqueued the Celery message INSIDE the
request transaction (before get_db's teardown commit). When a worker won the
race it ran the atomic claim before the row was committed, got rowcount=0 and
bailed, leaving the row stuck in `pending` forever (started_at stays NULL,
retry_count stays 0; the watchdog deliberately skips fresh rc=0 pending). These
are dead — the message is long gone — so we mark them `failed` with a clear,
non-leaking reason. Users re-create any scrape they still want.

Safety:
  * Targets ONLY genuine orphans: status='pending' AND started_at IS NULL AND
    retry_count = 0. (A job a worker has claimed has started_at set.)
  * SURGICAL GUARD (Codex P1): the base predicate alone is too broad — a fresh,
    not-yet-claimed job also matches it. --commit therefore REFUSES to run
    without at least one of --ids (explicit job-id allowlist) or
    --created-before (created_at upper bound). Pass the known incident set so
    this can never mark a legitimately fresh pending job as failed.
  * The UPDATE repeats the same predicate as a compare-and-set, so a row a
    worker claims between the SELECT and the UPDATE is left untouched.
  * Raw parameterized UPDATE — no ORM events, no Celery side-effects, no
    billing/delivery hooks fire.
  * Dry-run by default. Pass --commit to write.

Run on prod env:
    # dry-run (no guard needed to preview)
    railway run --service worker python scripts/failclean_orphaned_pending_jobs.py
    # surgical commit — only the listed ids, only if created before the cutoff
    railway run --service worker python scripts/failclean_orphaned_pending_jobs.py \
        --ids id1,id2,id3 --created-before '2026-06-16 07:00:00+00' --commit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REASON = (
    "Job was orphaned in 'pending' and never started: the create request's "
    "queue message raced ahead of the database commit, so no worker could "
    "claim it. Closed by maintenance cleanup — please re-create this scrape "
    "to run it again."
)


def main():
    import argparse

    from sqlalchemy import text

    from src.db.session import system_sync_session

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply the UPDATE (default: dry-run)")
    ap.add_argument(
        "--ids",
        default="",
        help="comma-separated job-id allowlist; only these orphans are touched (surgical)",
    )
    ap.add_argument(
        "--created-before",
        default="",
        help="ISO timestamp upper bound (e.g. '2026-06-16 07:00:00+00'); only orphans "
        "created strictly before this are touched",
    )
    args = ap.parse_args()

    ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    cutoff = args.created_before.strip() or None

    # Codex P1: a broad fail-clean can mark a legitimately fresh, not-yet-claimed
    # job as failed. Refuse to --commit without at least one surgical guard
    # (explicit id allowlist and/or a created_at upper bound).
    if args.commit and not ids and not cutoff:
        print(
            "ABORT: --commit requires a surgical guard. Pass --ids and/or "
            "--created-before so this can only touch the known incident set, "
            "never a fresh pending job."
        )
        sys.exit(2)

    # Base orphan predicate + optional surgical filters. The same predicate is
    # repeated in the UPDATE as a compare-and-set, so a row a worker claims
    # between SELECT and UPDATE is left untouched.
    where = ["status = 'pending'", "started_at IS NULL", "retry_count = 0"]
    params: dict = {}
    if ids:
        where.append("id::text = ANY(:ids)")  # jobs.id is uuid; cast to text to match the str allowlist
        params["ids"] = ids
    if cutoff:
        where.append("created_at < :cutoff")
        params["cutoff"] = cutoff
    where_sql = "\n          AND ".join(where)

    # where_sql is composed ONLY of fixed literal predicates; the id allowlist and
    # cutoff are bound params (:ids, :cutoff) — never string-interpolated. Safe.
    select_sql = text(
        "SELECT count(*) AS n, min(created_at) AS oldest, "  # noqa: S608 — fixed literals + bound params only
        "max(created_at) AS newest, count(DISTINCT user_id) AS users "
        f"FROM jobs WHERE {where_sql}"
    )
    list_sql = text(
        f"SELECT id::text, user_id::text, created_at FROM jobs WHERE {where_sql} "  # noqa: S608 — fixed literals + bound params only
        "ORDER BY created_at"
    )
    update_sql = text(
        "UPDATE jobs SET status = 'failed', error_message = :reason, finished_at = now() "  # noqa: S608 — fixed literals + bound params only
        f"WHERE {where_sql}"
    )

    with system_sync_session() as db:
        row = db.execute(select_sql, params).one()
        print("== Orphaned pending candidates (after guards) ==")
        print(f"  ids guard      : {ids or '(none)'}")
        print(f"  created-before : {cutoff or '(none)'}")
        print(f"  count          : {row.n}")
        print(f"  distinct users : {row.users}")
        print(f"  oldest created : {row.oldest}")
        print(f"  newest created : {row.newest}")
        for r in db.execute(list_sql, params).fetchall():
            print(f"    - id={r[0]} user={r[1]} created={r[2]}")
        print()

        if row.n == 0:
            print("  Nothing to do.")
            return

        if not args.commit:
            print("  DRY-RUN — no changes written. Re-run with --commit to apply.")
            return

        result = db.execute(update_sql, {"reason": REASON, **params})
        db.commit()
        print(f"  COMMITTED — marked {result.rowcount} job(s) as failed.")

        # Re-check what (if anything) is still active under the same guards
        remaining = db.execute(select_sql, params).one()
        print(f"  remaining orphaned pending (guarded): {remaining.n}")


if __name__ == "__main__":
    main()
