"""Recompute ``users.records_used`` from the durable per-job billing ledger.

WHY THIS EXISTS
---------------
Two defects in the monthly quota rollover destroyed usage counters without
touching the underlying billing record (see migration 086 and
``src/workers/scheduler_helpers/billing.py``):

  1. ``records_period_start`` was NULL on every newly registered user, so the
     rollover's ``IS NULL`` arm zeroed their counter inside their own signup
     month.
  2. The rollover zeroed unconditionally, so a late catch-up run (after Beat
     missed the 1st) also wiped usage already billed inside the NEW period.

Neither touched ``jobs.billed_count`` / ``jobs.billing_applied_at`` — the
per-job anchor written under a compare-and-set at the moment the user was
actually charged. So the correct counter is not a guess: it is

    records_used = SUM(jobs.billed_count)
                   WHERE billing_applied_at IS NOT NULL
                     AND billing_applied_at >= users.records_period_start

Jobs that failed before billing have ``billing_applied_at IS NULL`` and a
``billed_count`` of 0, so they correctly contribute nothing — a failed scrape
was never charged and must not be charged retroactively.

SAFETY
------
* Dry-run by default. ``--commit`` additionally requires ``--i-understand``.
* Only ever writes ``users.records_used``. Never touches plan, limit, period,
  Stripe state, jobs, or results.
* Refuses to run if any candidate user has a NULL ``records_period_start``,
  because the period is what scopes the SUM — without it the "current period"
  is undefined and the recomputed number would be meaningless. Run migration
  086 first.
* SKIPS users whose ENTITLEMENT WINDOW has ended (``quota_period_end <= now``).
  The SUM is scoped ``>= records_period_start``, so on a window that has already
  expired it would sweep in the PREVIOUS window's jobs and over-count. Let the
  rollover advance the window first, then re-run.

  Migration 088 is why this is a window test and no longer a calendar-month one.
  Quota resets on each user's own entitlement anniversary, so a perfectly live
  window can START in a previous calendar month — a subscriber anchored on the
  20th is legitimately mid-window on the 6th. The old ``records_period_start <
  date_trunc('month', now())`` test would have called every such account stale
  and silently refused to repair any of them.
* INCREASES ONLY by default. The defect being repaired under-counts, so an
  increase restores lost usage. A ledger BELOW the stored counter is a different
  thing entirely and is usually legitimate: deleting a job removes its
  ``billed_count`` from the ledger, but deleting data must never refund consumed
  quota (see ``tests/test_quota_accounting.py``). Decreasing would hand back
  quota to anyone who deleted a job. ``--allow-decrease`` opts in explicitly.
* Each write is guarded on the value we measured (optimistic concurrency), so a
  job that bills concurrently mid-run cannot be silently clobbered; the row is
  skipped and reported instead.
* Idempotent: a second run is a no-op because the ledger has not changed.

USAGE
-----
    railway run python scripts/repair_records_used_from_ledger.py
    railway run python scripts/repair_records_used_from_ledger.py --commit --i-understand
    ... [--user-id <uuid>] to scope to a single account
"""

from __future__ import annotations

import argparse
import os
import sys

# Run directly (`railway run python scripts/...`) as the docstring documents,
# without needing PYTHONPATH set: put the repo root on the path first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

# The stored counter next to what the billing anchor says it should be, for
# every user, scoped to that user's OWN current period.
_AUDIT_SQL = """
    SELECT u.id                                        AS user_id,
           u.plan                                      AS plan,
           u.records_used                              AS stored,
           u.records_limit                             AS records_limit,
           u.records_period_start                      AS period_start,
           COALESCE(SUM(j.billed_count) FILTER (
               WHERE j.billing_applied_at IS NOT NULL
                 AND j.billing_applied_at >= u.records_period_start
           ), 0)                                       AS ledger,
           (u.quota_period_end <= NOW())               AS period_is_stale
    FROM users u
    LEFT JOIN jobs j ON j.user_id = u.id
    {user_filter}
    GROUP BY u.id, u.plan, u.records_used, u.records_limit, u.records_period_start
    ORDER BY u.records_period_start, u.id
"""

_NULL_PERIOD_SQL = """
    SELECT COUNT(*) FROM users
    WHERE records_period_start IS NULL {user_filter_and}
"""


def _audit(db, user_id: str | None) -> list[dict]:
    sql = _AUDIT_SQL.format(
        user_filter="WHERE u.id = CAST(:uid AS uuid)" if user_id else ""
    )
    params = {"uid": user_id} if user_id else {}
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def _assert_periods_populated(db, user_id: str | None) -> None:
    sql = _NULL_PERIOD_SQL.format(
        user_filter_and="AND id = CAST(:uid AS uuid)" if user_id else ""
    )
    params = {"uid": user_id} if user_id else {}
    null_periods = db.execute(text(sql), params).scalar() or 0
    if null_periods:
        raise SystemExit(
            f"REFUSING: {null_periods} user(s) still have a NULL "
            f"records_period_start. The period is what scopes the ledger SUM, so "
            f"the recomputed value would be meaningless. Run `alembic upgrade "
            f"head` (migration 086) first, then re-run this script."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="apply the writes (default is a dry run)")
    ap.add_argument("--i-understand", action="store_true",
                    help="required with --commit: this rewrites billing counters")
    ap.add_argument("--user-id", default=None,
                    help="restrict to a single user id (default: all users)")
    ap.add_argument("--allow-decrease", action="store_true",
                    help="also LOWER counters whose ledger is below the stored "
                         "value. Off by default: a deleted job drops out of the "
                         "ledger, and deleting data must not refund quota.")
    args = ap.parse_args()

    if args.commit and not args.i_understand:
        raise SystemExit(
            "REFUSING --commit without --i-understand: this rewrites "
            "users.records_used, which gates plan enforcement and billing."
        )

    with system_sync_session() as db:
        _assert_periods_populated(db, args.user_id)
        rows = _audit(db, args.user_id)

        # An EXPIRED entitlement window would scope the SUM back into the
        # previous window and over-count. Report and exclude; the rollover (lazy,
        # or the hourly reconcile_quota_periods) will advance it.
        stale = [r for r in rows if r["period_is_stale"]]
        for r in stale:
            print(
                f"  SKIPPING {str(r['user_id'])[:8]}: entitlement window "
                f"starting {str(r['period_start'])[:10]} has ENDED, so the ledger SUM would "
                f"include last month. Let the rollover advance it, then re-run."
            )

        candidates = [r for r in rows if not r["period_is_stale"]]
        drifted = [r for r in candidates if r["ledger"] != r["stored"]]

        if not args.allow_decrease:
            decreases = [r for r in drifted if r["ledger"] < r["stored"]]
            for r in decreases:
                print(
                    f"  NOT LOWERING {str(r['user_id'])[:8]}: ledger "
                    f"{r['ledger']} < stored {r['stored']}. Usually a deleted "
                    f"job — deleting data must not refund quota. "
                    f"Use --allow-decrease to override."
                )
            drifted = [r for r in drifted if r["ledger"] > r["stored"]]

        print(f"\nUsers examined: {len(rows)}   skipped (stale period): "
              f"{len(stale)}   to repair: {len(drifted)}")
        if not drifted:
            print("Nothing to repair.")
            return 0

        under = sum(r["ledger"] - r["stored"] for r in drifted
                    if r["ledger"] > r["stored"])
        over = sum(r["stored"] - r["ledger"] for r in drifted
                   if r["stored"] > r["ledger"])
        print(f"Under-counted: {under} records   over-counted: {over} records\n")

        for r in drifted:
            delta = r["ledger"] - r["stored"]
            flag = ""
            if r["records_limit"] != -1 and r["ledger"] > r["records_limit"]:
                flag = f"  !! ledger EXCEEDS limit {r['records_limit']}"
            print(
                f"  {str(r['user_id'])[:8]}  plan={r['plan']:<9} "
                f"stored={r['stored']:<7} ledger={r['ledger']:<7} "
                f"delta={delta:+7}  period={str(r['period_start'])[:10]}{flag}"
            )

        if not args.commit:
            print("\nDRY RUN — nothing written. Re-run with --commit --i-understand.")
            return 0

        print("\nApplying…")
        repaired = skipped = 0
        for r in drifted:
            # Guard on the value we measured: if a job billed this user between
            # the audit and now, the row no longer matches and we skip it rather
            # than overwrite a fresh, correct increment with a stale total.
            # Guard on the period TOO, not just the counter. A concurrent job
            # billing ZERO records rolls records_period_start forward while
            # leaving records_used untouched — a counter-only guard would still
            # match and would write a total computed for the OLD period into the
            # new one. (Codex)
            moved = db.execute(
                text(
                    "UPDATE users SET records_used = :ledger "
                    "WHERE id = CAST(:uid AS uuid) "
                    "  AND records_used = :stored "
                    "  AND records_period_start = :period"
                ),
                {"ledger": r["ledger"], "uid": str(r["user_id"]),
                 "stored": r["stored"], "period": r["period_start"]},
            ).rowcount
            if moved == 1:
                repaired += 1
            else:
                skipped += 1
                print(
                    f"  SKIPPED {str(r['user_id'])[:8]}: records_used changed "
                    f"under us (was {r['stored']} @ {str(r['period_start'])[:10]}). "
                    f"Re-run to pick it up."
                )
        db.commit()
        print(f"\nRepaired {repaired} user(s); skipped {skipped}.")

        # Prove convergence from a fresh read rather than assuming the writes
        # landed — the same discipline the earlier backfill incidents demanded.
        remaining = [
            x for x in _audit(db, args.user_id)
            if not x["period_is_stale"] and x["ledger"] > x["stored"]
        ]
        print(f"Post-repair under-count remaining: {len(remaining)} user(s)")
        return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
