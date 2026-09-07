"""Step-2 deploy verification: prove migration 088 was a behavioural NO-OP.

WHAT THIS ANSWERS
-----------------
The entitlement deploy is only safe to follow with ``backfill_quota_anchors.py``
if the migration moved nobody. This script is the evidence for that gate. It is
STRICTLY READ-ONLY -- it opens a session, runs SELECTs, and never issues an
UPDATE, INSERT or DDL statement.

THE TRAP THIS SCRIPT USED TO FALL INTO (fixed; read before editing)
-------------------------------------------------------------------
The first draft compared the new window against ``records_period_start`` and
claimed no pre-deploy snapshot was needed, because that column holds the pre-088
value. **That is wrong.** ``window_set_sql`` in ``src/api/quota_window.py`` ends
with ``records_period_start = w.new_start`` -- the legacy column is written in
LOCKSTEP with ``quota_period_start`` on every rollover. So as soon as any quota
writer rolls a user, C1/C2 compare the new value against itself and PASS
VACUOUSLY. Silently correct right after the migration, silently wrong later:
exactly the class of bug this whole project exists to remove.

Two independent defences, so the gate cannot pass vacuously:

* **C7 (always on, no snapshot needed).** Migration 088 backfills
  ``quota_anchor_at = quota_period_start = records_period_start``, so
  immediately afterwards EVERY user satisfies ``quota_period_start ==
  quota_anchor_at``. A rollover moves the window off the anchor and leaves it
  there. C7 therefore detects "this user has rolled since the migration" exactly
  -- and when it fails, **C1/C2 are reported as UNVERIFIABLE for that user
  rather than passing**, because the baseline they compare against may already
  have been overwritten.

* **``--baseline`` (authoritative).** Take a snapshot BEFORE merging, then pass
  it here. It is the only thing that still works once users have rolled, and the
  only way to say anything at all about ``records_used``.

WHAT A CLEAN RUN LOOKS LIKE
---------------------------
Every check reports 0 offenders and the script exits 0. Any non-zero count means
STOP -- do not run the anchor backfill.

    C1  quota_period_start  == pre-deploy period start   (window unmoved)
    C2  quota_anchor_at     == pre-deploy period start   (day-1 grid)
    C3  quota_period_end    == quota_period_start + 1 month
    C4  anchor lands on day 1 of a month                 (legacy behaviour)
    C5  effective window    == stored window             (nothing already stale)
    C6  no NULLs in the new NOT NULL columns
    C7  quota_period_start  == quota_anchor_at           (nobody has rolled yet)
    C8  records_used vs baseline    (--baseline; INFORMATIONAL unless
                                     --strict-counter, see below)

C5, C7 and C8 can legitimately be non-zero if time has passed since the
migration: a user whose window ended has rolled, and live traffic moves the
counter. Those are the lazy rollover and ordinary usage working as designed, so
they are reported SEPARATELY from hard failures. Two consequences:

* C7 failures DO downgrade C1/C2 to UNVERIFIABLE, which IS a hard failure unless
  a baseline was supplied. Run this promptly after the deploy, or use --baseline.
* C8 is REPORTED, never judged, because against live traffic no delta is
  diagnostic in either direction. Up is a reservation or settlement; down is
  equally ordinary, since settling fewer delivered records than were reserved
  charges ``billable - reserved`` and a release subtracts the whole reservation,
  both with C7 still holding. There is no shape to key on, so the only honest
  options are to report it or to quiesce.
  What the script does NOT do is let that slide by quietly: whenever a delta
  exists the final verdict reads "WINDOWS verified ... records_used NOT
  verified" and names how many users drifted, instead of an unqualified "moved
  nobody" that would walk a counter regression straight through the gate.
  ``--strict-counter`` fails on any movement and is sound only against a
  genuinely quiesced production.

USAGE
-----
    # BEFORE merging (against the pre-088 production DB):
    railway run python scripts/verify_entitlement_deploy.py --snapshot pre088.json

    # AFTER the migration:
    railway run python scripts/verify_entitlement_deploy.py --baseline pre088.json

    # without a baseline (only trustworthy while C7 holds for everyone):
    railway run python scripts/verify_entitlement_deploy.py

    # a specific account, e.g. the known over-cap one:
    railway run python scripts/verify_entitlement_deploy.py --user 01dc9396

Exit codes: 0 = clean, 1 = at least one hard check failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.api.quota_window import add_months, as_utc, effective_window  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402

# The account the handoff calls out by name. Printed in full whether or not it
# passes, because it is the one row an operator should eyeball by hand.
WATCHED_USER_PREFIX = "01dc9396"

# Its expected counter. Was 1007 through the 088 deploy (1001 from an earlier
# incident repair + 6 from a live reservation canary), and 1007 was confirmed
# twice in production AFTER the migration ran. It later read 1001 — the
# canary's +6 reverted by something outside this deploy, during a window whose
# logs no longer reach back. **The operator confirmed 1001 is the intended
# value on 2026-09-07**, so that is what this asserts.
#
# Still over the 1000 limit either way, which is the property that actually
# matters: this account must stay BLOCKED until its window rolls on 2026-10-01.
# Do not "fix" it to a nicer number in either direction without asking.
WATCHED_EXPECTED_USED = 1001
WATCHED_EXPECTED_LIMIT = 1000


def _fmt(value: datetime | None) -> str:
    if value is None:
        return "NULL"
    return as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return as_utc(datetime.fromisoformat(value))


def take_snapshot(path: str) -> int:
    """Record the PRE-088 state. Safe to run before the migration exists.

    Deliberately raw SQL over only the legacy columns: the ORM ``User`` model
    already declares the 088 columns, so ``select(User)`` would fail against a
    pre-migration database -- which is precisely when this must run.
    """
    try:
        with system_sync_session() as db:
            rows = db.execute(
                text(
                    "SELECT id, email, plan, records_used, records_limit, "
                    "records_period_start FROM users"
                )
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - report, never mask
        print(f"FATAL: could not read users: {exc!r}", file=sys.stderr)
        return 2

    if not rows:
        print("FATAL: zero users read -- refusing to write an empty baseline.", file=sys.stderr)
        return 2

    payload = {
        "taken_at": datetime.now(UTC).isoformat(),
        "users": {
            str(r["id"]): {
                "email": r["email"],
                "plan": r["plan"],
                "records_used": r["records_used"],
                "records_limit": r["records_limit"],
                "records_period_start": (
                    as_utc(r["records_period_start"]).isoformat()
                    if r["records_period_start"] is not None
                    else None
                ),
            }
            for r in rows
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Snapshot written: {path}  ({len(rows)} users, taken {payload['taken_at']})")
    print("Keep this file. Pass it back with --baseline after the migration.")
    return 0


def _check_user(
    user: SimpleNamespace,
    now: datetime,
    base: dict | None,
    strict_counter: bool,
    counter_drift: list,
) -> tuple[list[str], list[str]]:
    """Return (hard_failures, informational_notes) for one user."""
    failures: list[str] = []
    notes: list[str] = []

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

    # C7 -- has this user rolled since the migration? The 088 backfill sets
    # quota_period_start == quota_anchor_at for everyone, and only a rollover
    # moves the window off the anchor.
    rolled_since_migration = qps_u != anchor_u
    if rolled_since_migration:
        notes.append(
            f"C7 window has advanced past the anchor: quota_period_start={_fmt(qps_u)} "
            f"!= quota_anchor_at={_fmt(anchor_u)} (this user has rolled since 088)"
        )

    # C1 / C2 -- the baseline comparison.
    #
    # The two checks do NOT have the same validity once a user has rolled, and
    # conflating them is what made the first draft wrong:
    #
    #   C1 (window == pre-deploy start) is only meaningful while C7 holds. A
    #       rollover moves the window LEGITIMATELY, so after one there is no way
    #       to observe where the migration left it -- with or without a baseline.
    #   C2 (anchor == pre-deploy start) stays meaningful forever, because the
    #       anchor is immutable across rollovers by design (it moves on exactly
    #       three events, none of which is a rollover). It only needs a
    #       TRUSTWORTHY pre-deploy value to compare against.
    #
    # The in-row legacy column is trustworthy only while C7 holds, because
    # window_set_sql rewrites records_period_start in lockstep on every roll.
    if base is not None:
        baseline_start = _parse_iso(base.get("records_period_start"))
        baseline_source = "baseline snapshot"
    elif not rolled_since_migration:
        baseline_start = (
            as_utc(user.records_period_start)
            if user.records_period_start is not None
            else None
        )
        baseline_source = "records_period_start (C7 holds, so still pre-088)"
    else:
        baseline_start = None
        baseline_source = None
        failures.append(
            "C2 UNVERIFIABLE: this user has rolled since the migration (C7), so "
            "records_period_start was rewritten in lockstep with quota_period_start "
            "and is no longer the pre-088 value. There is nothing trustworthy left "
            "in the row to compare the anchor against. Re-run with --baseline "
            "<snapshot taken before the deploy> to check this user."
        )

    if baseline_start is None and baseline_source is not None:
        notes.append(f"C1/C2 skipped: no pre-deploy period start in {baseline_source}")
    elif baseline_start is not None:
        if rolled_since_migration:
            notes.append(
                "C1 not applicable: the window has legitimately rolled since the "
                "migration, so its migration-time position is no longer observable. "
                "C2 (anchor) still applies and was checked."
            )
        elif qps_u != baseline_start:
            failures.append(
                f"C1 window MOVED: quota_period_start={_fmt(qps_u)} "
                f"!= pre-deploy {_fmt(baseline_start)} [{baseline_source}]"
            )
        if anchor_u != baseline_start:
            failures.append(
                f"C2 anchor != pre-deploy period start: quota_anchor_at={_fmt(anchor_u)} "
                f"!= {_fmt(baseline_start)} [{baseline_source}]"
            )

    # C8 -- the counter. Only a baseline can say anything about it at all.
    #
    # INFORMATIONAL BY DEFAULT, and that is not laziness. The deploy does not
    # quiesce production, so between the snapshot and this run a reservation can
    # legitimately raise records_used, a release can lower it, and a rollover can
    # zero it. Treating any diff as a migration failure would block a perfectly
    # good deploy on ordinary traffic -- the gate would cry wolf and get ignored,
    # which is worse than not having it.
    #
    # Use --strict-counter only when production really is quiesced; then any
    # diff IS the migration and should stop the deploy.
    # Against LIVE traffic, no delta is diagnostic -- in either direction. Up is
    # a reservation or settlement. Down is just as ordinary: settling fewer
    # delivered records than were reserved charges ``billable - reserved``, and
    # release_quota_reservation() subtracts the whole reservation, both inside
    # the current window with C7 still holding. An earlier revision hard-failed
    # on a decrease, which would have stopped the deploy on a job that reserved
    # 200 and delivered 150. There is no shape to key on.
    #
    # So the delta is REPORTED, never judged, and the final verdict refuses to
    # claim the counter was verified whenever one exists. --strict-counter is
    # the only sound way to actually check it, and it is only sound because it
    # presumes a quiesced production where nothing else can move the number.
    if base is not None and base.get("records_used") is not None:
        before, after = base["records_used"], user.records_used
        if before != after:
            delta = after - before
            moved = f"C8 records_used moved {before} -> {after} ({delta:+d}) since the snapshot"
            if strict_counter:
                failures.append(
                    f"{moved}. [--strict-counter] Production was declared quiesced, "
                    "so any movement is the migration."
                )
            else:
                notes.append(
                    f"{moved}. Ordinary under live traffic in EITHER direction (reserve, "
                    "settle-for-less, release, or a rollover reset); migration 088 does "
                    "not touch this column. NOT proof the migration left it alone -- only "
                    "a quiesced run with --strict-counter can show that."
                )
                counter_drift.append(user)

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
        "--snapshot",
        default=None,
        metavar="PATH",
        help="PRE-deploy mode: write the current legacy state to PATH and exit.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        metavar="PATH",
        help="Compare against a snapshot taken before the deploy (authoritative).",
    )
    parser.add_argument(
        "--strict-counter",
        action="store_true",
        help=(
            "Treat any records_used difference from the baseline as a hard failure. "
            "Only correct when production is quiesced -- under live traffic the "
            "counter moves for legitimate reasons and this will fail spuriously."
        ),
    )
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

    if args.snapshot:
        if args.baseline:
            print("FATAL: --snapshot and --baseline are mutually exclusive.", file=sys.stderr)
            return 2
        return take_snapshot(args.snapshot)

    baseline: dict[str, dict] | None = None
    if args.baseline:
        try:
            with open(args.baseline, encoding="utf-8") as fh:
                loaded = json.load(fh)
            baseline = loaded["users"]
        except Exception as exc:  # noqa: BLE001 - a bad baseline must not be ignored
            print(f"FATAL: could not read baseline {args.baseline}: {exc!r}", file=sys.stderr)
            return 2
        if not baseline:
            print("FATAL: baseline contains no users.", file=sys.stderr)
            return 2

    now = datetime.now(UTC)
    # Cross-tenant read: this is a system-level audit of every user, so it must
    # run without an RLS context. Under a per-user session the SELECT returns
    # zero rows and the script would report a vacuous PASS -- which is why the
    # empty result below is a FATAL, not a clean run.
    #
    # Raw SQL over named columns rather than select(User), for the same reason
    # take_snapshot() does it: the ORM model maps `email` through the field
    # encryptor, and a single legacy row with an unencrypted value aborts the
    # WHOLE read with InvalidToken under strict mode. A quota audit has no
    # business decrypting anybody's email -- it never prints one -- so it does
    # not load the column at all. (Observed against production: this exact
    # failure, on a deploy where nothing was wrong with the quota data.)
    try:
        with system_sync_session() as db:
            rows = db.execute(
                text(
                    "SELECT id, plan, records_used, records_limit, "
                    "records_period_start, quota_anchor_at, quota_period_start, "
                    "quota_period_end, subscription_status, entitlement_ends_at, "
                    "entitlement_grace_ends_at FROM users"
                )
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - report, never mask, a connect failure
        print(f"FATAL: could not read users: {exc!r}", file=sys.stderr)
        return 2

    users = [SimpleNamespace(**dict(r)) for r in rows]

    if args.user:
        users = [u for u in users if str(u.id).startswith(args.user)]

    total = len(users)
    if total == 0:
        print("FATAL: no users matched -- refusing to report a vacuous PASS.", file=sys.stderr)
        return 2

    counter_drift: list[SimpleNamespace] = []
    failed: list[tuple[SimpleNamespace, list[str]]] = []
    rolled: list[tuple[SimpleNamespace, list[str]]] = []
    watched: SimpleNamespace | None = None
    missing_from_baseline: list[SimpleNamespace] = []

    for user in users:
        if str(user.id).startswith(WATCHED_USER_PREFIX):
            watched = user
        base = baseline.get(str(user.id)) if baseline is not None else None
        if baseline is not None and base is None:
            # A user created after the snapshot. Not a defect, but C1/C2/C8
            # cannot be judged for them, so say so instead of passing quietly.
            missing_from_baseline.append(user)
        failures, notes = _check_user(
            user, now, base, args.strict_counter, counter_drift
        )
        if failures:
            failed.append((user, failures))
        if notes:
            rolled.append((user, notes))

    print("=" * 72)
    print("ENTITLEMENT DEPLOY VERIFICATION (step 2) -- READ-ONLY")
    print(f"clock: {_fmt(now)}   users examined: {total}")
    print(
        "baseline: "
        + (f"{args.baseline} ({len(baseline)} users)" if baseline else "NONE (relying on C7)")
    )
    print("=" * 72)

    shown = 0
    for user, failures in failed:
        if args.limit and shown >= args.limit:
            print(f"... {len(failed) - shown} more failing users not shown (--limit)")
            break
        print(f"\nFAIL  {user.id}  plan={user.plan}")
        for line in failures:
            print(f"      {line}")
        shown += 1

    if missing_from_baseline:
        print(
            f"\n{len(missing_from_baseline)} user(s) are absent from the baseline "
            "(created after the snapshot). C1/C2/C8 were not judged for them."
        )
        for user in missing_from_baseline[: args.limit or len(missing_from_baseline)]:
            print(f"  {user.id}")

    if rolled:
        print(f"\n{len(rolled)} user(s) carry an informational note (C5 / C7).")
        print("A window that has advanced is the LAZY ROLLOVER working, not a defect:")
        print("it advances inside the next statement that charges them, or hourly via")
        print("reconcile_quota_periods. It only invalidates C1/C2 -- which is why those")
        print("become hard failures above unless --baseline was supplied.")
        for user, notes in rolled[: args.limit or len(rolled)]:
            print(f"  {user.id}")
            for line in notes:
                print(f"      {line}")

    if watched is not None:
        eff_start, eff_end = effective_window(watched, now)
        print("\n" + "-" * 72)
        print(f"WATCHED ACCOUNT  {watched.id}")
        print(f"  plan            : {watched.plan}")
        print(f"  records_used    : {watched.records_used}")
        print(f"  records_limit   : {watched.records_limit}")
        print(f"  over cap        : {watched.records_used > watched.records_limit}")
        print(f"  records_period_start : {_fmt(watched.records_period_start)}")
        print(f"  quota_anchor_at      : {_fmt(watched.quota_anchor_at)}")
        print(
            f"  stored window        : [{_fmt(watched.quota_period_start)} "
            f"-> {_fmt(watched.quota_period_end)})"
        )
        print(f"  effective window     : [{_fmt(eff_start)} -> {_fmt(eff_end)})")
        print(
            f"  EXPECTED: records_used {WATCHED_EXPECTED_USED}, "
            f"limit {WATCHED_EXPECTED_LIMIT}, over cap True,"
        )
        print("            next reset 2026-10-01. Do NOT 'fix' this number.")
        if watched.records_used != WATCHED_EXPECTED_USED:
            # Loud, but not a hard failure: the expected value is a hand-maintained
            # constant, and this account legitimately moves the day it converts to
            # paid (P1 re-anchors and zeroes it). Say so rather than either passing
            # silently or blocking a deploy on a stale literal.
            print(
                f"  ** MISMATCH: reads {watched.records_used}, expected "
                f"{WATCHED_EXPECTED_USED}. Confirm which is intended before "
                "trusting this run, and update WATCHED_EXPECTED_USED."
            )
        if watched.records_used <= watched.records_limit:
            print(
                "  ** This account is NO LONGER over cap. That is a real state "
                "change -- it should stay blocked until 2026-10-01."
            )
        print("-" * 72)
    else:
        print(f"\nNOTE: no user id starting {WATCHED_USER_PREFIX!r} was found.")

    print("\n" + "=" * 72)
    print(f"RESULT: {total - len(failed)}/{total} users pass all hard checks.")
    if failed:
        print(f"        {len(failed)} FAILING -- STOP. Do not run backfill_quota_anchors.py.")
    elif counter_drift:
        # Never print an unqualified "moved nobody" while a counter mismatch is
        # sitting in the notes: the windows were verified, records_used was NOT,
        # and saying otherwise would let the quota-counter regression this gate
        # exists for walk straight through step 2.
        print("        WINDOWS verified: migration 088 moved nobody's window or anchor.")
        print(
            f"        records_used NOT verified -- it moved for {len(counter_drift)} "
            "user(s) since the snapshot."
        )
        print("        Live traffic explains that, but this run cannot prove it. To")
        print("        verify the counter, re-run against a quiesced production with")
        print("        --strict-counter, or read the C8 notes above and satisfy yourself")
        print("        each delta is ordinary usage before running the anchor backfill.")
    else:
        print("        Migration 088 moved nobody. Step 2 verified.")
    print(f"        {len(rolled)} with an informational C5/C7 note.")
    print("=" * 72)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
