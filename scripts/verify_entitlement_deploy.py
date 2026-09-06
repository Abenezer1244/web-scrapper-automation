"""Step-2 deploy verification: prove migration 088 was a behavioural NO-OP.

WHAT THIS ANSWERS
-----------------
The entitlement deploy is only safe to follow with ``backfill_quota_anchors.py``
if the migration moved nobody. This script is the evidence for that gate. It is
STRICTLY READ-ONLY -- it opens a session, runs SELECTs, and never issues an
UPDATE, INSERT or DDL statement.

WHY IT CAN CHECK "the same window they already had" WITHOUT A PRE-SNAPSHOT
--------------------------------------------------------------------------
``records_period_start`` is the PRE-088 column and is deliberately kept, written
in lockstep with ``quota_period_start`` for one release. Migration 088 backfills

    quota_anchor_at = quota_period_start = records_period_start
    quota_period_end = records_period_start + 1 month

so the old value is still present in the same row. Comparing the new window
against ``records_period_start`` IS the "did anyone move?" test, and it needs no
snapshot taken beforehand. ``records_used`` is not touched by the migration at
all, so it is reported rather than diffed.

WHAT A CLEAN RUN LOOKS LIKE
---------------------------
Every check reports 0 offenders and the script exits 0. Any non-zero count means
STOP -- do not run the anchor backfill.

    C1  quota_period_start  == records_period_start          (window unmoved)
    C2  quota_anchor_at     == records_period_start          (day-1 grid)
    C3  quota_period_end    == quota_period_start + 1 month  (exactly one month)
    C4  anchor lands on day 1 of a month                     (legacy behaviour)
    C5  effective window    == stored window                 (nothing already
                                                              stale at deploy)
    C6  no NULLs in the new NOT NULL columns

C5 is the one that can legitimately be non-zero: a user whose window ended
between the migration and this run has an effective window ahead of the stored
one, which is the lazy rollover working as designed. Those are reported
SEPARATELY from failures for exactly that reason -- read the note it prints.

USAGE
-----
    railway run python scripts/verify_entitlement_deploy.py

    # a specific account, e.g. the known over-cap one:
    railway run python scripts/verify_entitlement_deploy.py --user 01dc9396

Exit codes: 0 = clean, 1 = at least one hard check failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src.api.quota_window import add_months, as_utc, effective_window  # noqa: E402
from src.db.models import User  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402

# The account the handoff calls out by name. Its 1007/1000 is CORRECT and must
# survive the deploy untouched; it is printed in full whether or not it passes.
WATCHED_USER_PREFIX = "01dc9396"


def _fmt(value: datetime | None) -> str:
    if value is None:
        return "NULL"
    return as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def _check_user(user: User, now: datetime) -> tuple[list[str], list[str]]:
    """Return (hard_failures, informational_notes) for one user."""
    failures: list[str] = []
    notes: list[str] = []

    rps = user.records_period_start
    qps = user.quota_period_start
    qpe = user.quota_period_end
    anchor = user.quota_anchor_at

    # C6 first -- everything below dereferences these.
    missing = [
        name
        for name, value in (
            ("quota_anchor_at", anchor),
            ("quota_period_start", qps),
            ("quota_period_end", qpe),
        )
        if value is None
    ]
    if missing:
        failures.append(f"C6 NULL in NOT NULL column(s): {', '.join(missing)}")
        return failures, notes

    anchor_u, qps_u, qpe_u = as_utc(anchor), as_utc(qps), as_utc(qpe)

    if rps is None:
        notes.append("C1 skipped: records_period_start is NULL (pre-existing)")
    else:
        rps_u = as_utc(rps)
        if qps_u != rps_u:
            failures.append(
                f"C1 window MOVED: quota_period_start={_fmt(qps_u)} "
                f"!= records_period_start={_fmt(rps_u)}"
            )
        if anchor_u != rps_u:
            failures.append(
                f"C2 anchor != legacy period start: quota_anchor_at={_fmt(anchor_u)} "
                f"!= records_period_start={_fmt(rps_u)}"
            )

    expected_end = add_months(qps_u, 1)
    if qpe_u != expected_end:
        failures.append(
            f"C3 window is not one month: quota_period_end={_fmt(qpe_u)} "
            f"!= quota_period_start + 1 month ({_fmt(expected_end)})"
        )

    if anchor_u.day != 1:
        failures.append(
            f"C4 anchor is NOT day 1: {_fmt(anchor_u)} -- the migration must leave "
            "everyone on a day-1 grid; a non-day-1 anchor before the legacy reset "
            "is retired would zero this user twice"
        )

    eff_start, eff_end = effective_window(user, now)
    if (eff_start, eff_end) != (qps_u, qpe_u):
        notes.append(
            f"C5 effective window has rolled ahead of stored: "
            f"stored [{_fmt(qps_u)} -> {_fmt(qpe_u)}) "
            f"effective [{_fmt(eff_start)} -> {_fmt(eff_end)})"
        )

    return failures, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user",
        default=None,
        help="Only report on users whose id starts with this prefix.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Print at most this many offending users (0 = all).",
    )
    args = parser.parse_args()

    now = datetime.now(UTC)
    # Cross-tenant read: this is a system-level audit of every user, so it must
    # run without an RLS context. Under a per-user session the SELECT returns
    # zero rows and the script would report a vacuous PASS -- which is why the
    # empty result below is a FATAL, not a clean run.
    try:
        with system_sync_session() as db:
            users = list(db.execute(select(User)).scalars())
    except Exception as exc:  # noqa: BLE001 - report, never mask, a connect failure
        print(f"FATAL: could not read users: {exc!r}", file=sys.stderr)
        return 2

    if args.user:
        users = [u for u in users if str(u.id).startswith(args.user)]

    total = len(users)
    if total == 0:
        print("FATAL: no users matched -- refusing to report a vacuous PASS.", file=sys.stderr)
        return 2

    failed: list[tuple[User, list[str]]] = []
    rolled: list[tuple[User, list[str]]] = []
    watched: User | None = None

    for user in users:
        if str(user.id).startswith(WATCHED_USER_PREFIX):
            watched = user
        failures, notes = _check_user(user, now)
        if failures:
            failed.append((user, failures))
        if notes:
            rolled.append((user, notes))

    print("=" * 72)
    print("ENTITLEMENT DEPLOY VERIFICATION (step 2) -- READ-ONLY")
    print(f"clock: {_fmt(now)}   users examined: {total}")
    print("=" * 72)

    shown = 0
    for user, failures in failed:
        if args.limit and shown >= args.limit:
            print(f"... {len(failed) - shown} more failing users not shown (--limit)")
            break
        print(f"\nFAIL  {user.id}  {user.email}  plan={user.plan}")
        for line in failures:
            print(f"      {line}")
        shown += 1

    if rolled:
        print(f"\n{len(rolled)} user(s) have an effective window ahead of the stored one.")
        print("This is the LAZY ROLLOVER working, not a defect: the window advances")
        print("inside the next statement that charges them, or hourly via")
        print("reconcile_quota_periods. It is only a problem if the stored window")
        print("ended long before this run and the beat is not running.")
        for user, notes in rolled[: args.limit or len(rolled)]:
            print(f"  {user.id}  {user.email}")
            for line in notes:
                print(f"      {line}")

    if watched is not None:
        eff_start, eff_end = effective_window(watched, now)
        print("\n" + "-" * 72)
        print(f"WATCHED ACCOUNT  {watched.id}  {watched.email}")
        print(f"  plan            : {watched.plan}")
        print(f"  records_used    : {watched.records_used}")
        print(f"  records_limit   : {watched.records_limit}")
        print(f"  over cap        : {watched.records_used > watched.records_limit}")
        print(f"  records_period_start : {_fmt(watched.records_period_start)}")
        print(f"  quota_anchor_at      : {_fmt(watched.quota_anchor_at)}")
        print(f"  stored window        : [{_fmt(watched.quota_period_start)} "
              f"-> {_fmt(watched.quota_period_end)})")
        print(f"  effective window     : [{_fmt(eff_start)} -> {_fmt(eff_end)})")
        print("  EXPECTED: records_used 1007, limit 1000, over cap True,")
        print("            next reset 2026-10-01. Do NOT 'fix' this number.")
        print("-" * 72)
    else:
        print(f"\nNOTE: no user id starting {WATCHED_USER_PREFIX!r} was found.")

    print("\n" + "=" * 72)
    print(f"RESULT: {total - len(failed)}/{total} users pass all hard checks.")
    if failed:
        print(f"        {len(failed)} FAILING -- STOP. Do not run backfill_quota_anchors.py.")
    else:
        print("        Migration 088 moved nobody. Step 2 verified.")
    print(f"        {len(rolled)} with a lazily-rolled effective window (informational).")
    print("=" * 72)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
