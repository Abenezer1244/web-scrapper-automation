"""Close the entitlement windows migration 088 stretched past a trial's end.

WHY THIS EXISTS
---------------
A trial is its own entitlement window, ``[signup, trial_ends_at)``. Registration
has stamped it that way since migration 088, and ``_expire_trials_impl`` relies
on it: when the trial lapses the plan drops to Starter, the window has already
ended, and the next charging statement (or the hourly reconciliation) rolls the
user onto a fresh Starter window at 0.

Migration 088's ``BACKFILL_WINDOWS`` did not know that. It moved EVERY existing
user onto ``[records_period_start, +1 month)``, including the accounts that were
mid-trial at the deploy. Their window ran to the 1st instead of to their trial
end, so when the trial lapsed the Pro-trial usage stayed in the live window
under a Starter limit. Production, 2026-09-15: an account whose 1,001 records
were all billed inside its Pro trial read **1,001 / 50** and was refused every
scrape until Oct 1, while an account that signed up two days after the deploy
rolled to 0 / 50 on its trial end.

Nothing charged anyone twice and nothing leaked between accounts: the counter
equals that account's own job ledger. The WINDOW is what was wrong.

THE REPAIR
----------
For each candidate the window's end is corrected to ``trial_ends_at`` and the
user is then rolled through the SAME shared SQL the worker charges with
(``window_cte_sql`` / ``window_set_sql`` in ``src/api/quota_window.py``), so the
new window, the zeroed counter and the ``records_period_start`` mirror are
exactly what a post-088 trial user gets. The anchor is not moved: it moves only
on the three approved events (``src/api/billing_entitlement.py``).

The population is CLOSED. The only writers of ``trial_ends_at`` are registration
(which sets the window end to it) and paid conversion (which clears it), and
every trial running at the 088 deploy has since ended. So this is a one-shot
repair, not a new standing reconciliation step with its own race surface.

SAFETY
------
* Dry run by default. The dry run executes the real statement and ROLLS BACK,
  so what it prints is what ``--commit`` would write. ``--commit`` additionally
  requires ``--i-understand``.
* Each user is locked ``FOR UPDATE NOWAIT`` and every condition is re-checked
  AFTER the lock, in fresh READ COMMITTED statements. A reserve or settlement
  holding the row is SKIPPED (re-run later), never waited on. Only the users
  row is locked; jobs are read, never locked, so the jobs -> users lock order
  the worker uses cannot be inverted.
* Refuses, and writes nothing, when zeroing could destroy real usage:
    - any job of the user is not terminal (its reserve or settlement could land
      on either side of the repair). Reading jobs without locking them is
      sufficient because the worker only reserves or settles a job whose
      non-terminal status it has already committed (``_set_status`` CAS);
    - a job carries a billed count with no billing timestamp (it could not be
      placed on either side of the trial end);
    - anything was billed or reserved at or after the trial end (that usage
      belongs to the post-trial window);
    - the counter is not exactly the ledger billed inside
      ``[quota_period_start, trial_ends_at)`` (the repair zeroes trial usage and
      nothing else);
    - the corrected window may not roll under ``quota_should_roll`` (frozen for
      non-payment, or a paid entitlement end in the way).
* The row lock is held from the re-check through the write, in one
  transaction, so nothing measured can change before the UPDATE applies.
* Idempotent: a repaired user's window starts at the trial end, so it is no
  longer a candidate.

USAGE
-----
    railway run python scripts/repair_trial_window_backfill.py
    railway run python scripts/repair_trial_window_backfill.py --commit --i-understand
    ... [--user-id <uuid>] to scope to a single account
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime

# Run directly (`railway run python scripts/...`) without PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from src.api.quota_window import window_cte_sql, window_set_sql  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402

#: SQLSTATE for FOR UPDATE NOWAIT finding the row held (lock_not_available).
_LOCK_NOT_AVAILABLE = "55P03"

#: Job statuses after which neither a reservation nor a settlement can run.
_TERMINAL = ("done", "failed", "cancelled")

#: The legacy shape, evaluated at one pinned clock reading. Used both for
#: discovery (unlocked) and for the re-check under the row lock.
_SUCCESS = frozenset({"would_repair", "repaired"})

_CANDIDATE_PREDICATE = (
    "u.trial_ends_at IS NOT NULL "
    "AND u.first_paid_at IS NULL "
    "AND u.trial_ends_at <= CAST(:at AS timestamptz) "
    "AND u.trial_ends_at > u.quota_period_start "
    "AND u.trial_ends_at < u.quota_period_end"
)

_FIND_SQL = (
    "SELECT u.id, u.plan, u.records_used, u.records_limit, u.trial_ends_at, "
    "       u.quota_period_start, u.quota_period_end "
    "FROM users u WHERE " + _CANDIDATE_PREDICATE + " {user_filter} "
    "ORDER BY u.trial_ends_at, u.id"
)

_LOCK_SQL = (
    "SELECT u.id, u.records_used, u.quota_period_start, u.quota_period_end, "
    "       u.trial_ends_at "
    "FROM users u WHERE u.id = CAST(:uid AS uuid) AND " + _CANDIDATE_PREDICATE
    + " FOR UPDATE NOWAIT"
)

_GUARDS_SQL = """
    SELECT
      COUNT(*) FILTER (WHERE j.status NOT IN :terminal)              AS in_flight,
      COUNT(*) FILTER (WHERE j.billed_count <> 0
                         AND j.billing_applied_at IS NULL)           AS malformed,
      COALESCE(SUM(j.billed_count) FILTER (
          WHERE j.billing_applied_at >= CAST(:trial_end AS timestamptz)), 0)
                                                                     AS billed_after,
      COALESCE(SUM(j.reserved_count) FILTER (
          WHERE j.reserved_at >= CAST(:trial_end AS timestamptz)), 0) AS reserved_after,
      COALESCE(SUM(j.billed_count) FILTER (
          WHERE j.billing_applied_at >= CAST(:start AS timestamptz)
            AND j.billing_applied_at <  CAST(:trial_end AS timestamptz)), 0)
                                                                     AS trial_ledger
    FROM jobs j
    WHERE j.user_id = CAST(:uid AS uuid)
"""

# The window's true end substituted for the stored one, then the worker's own
# rollover projection. ``rolling`` is part of the WHERE: a frozen account (or a
# paid entitlement end in the way) is left exactly as it was.
_REPAIR_SQL = (
    "WITH cur AS ("  # noqa: S608 -- splices only the shared window builders; every value is bound
    "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
    "         u.quota_period_start, u.trial_ends_at AS quota_period_end,"
    "         u.subscription_status, u.entitlement_grace_ends_at,"
    "         u.entitlement_ends_at, u.pending_plan, u.pending_records_limit"
    "  FROM users u WHERE u.id = CAST(:uid AS uuid)"
    "), w AS ("
    "  SELECT cur.*, " + window_cte_sql("", ":at") + " FROM cur"
    ") UPDATE users u SET"
    "    records_used = w.base,"
    + window_set_sql("w")
    + "  FROM w WHERE u.id = w.id AND w.rolling"
    "  RETURNING w.new_start, w.new_end, u.records_used, u.records_limit"
)


@dataclass(frozen=True)
class Outcome:
    user_id: str
    status: str
    detail: str = ""
    records_used_before: int | None = None
    new_period_start: datetime | None = None
    new_period_end: datetime | None = None


def find_candidates(db, user_id: str | None = None) -> list:
    """Users on the legacy shape right now. Unlocked; ``repair_user`` re-checks."""
    at = db.execute(text("SELECT clock_timestamp()")).scalar()
    sql = _FIND_SQL.format(user_filter="AND u.id = CAST(:uid AS uuid)" if user_id else "")
    params = {"at": at, "uid": user_id} if user_id else {"at": at}
    rows = db.execute(text(sql), params).fetchall()
    db.rollback()
    return rows


def repair_user(db, user_id: str, *, commit: bool) -> Outcome:
    """Repair one user in its own transaction. Commits only when ``commit``."""
    from sqlalchemy import bindparam

    try:
        at = db.execute(text("SELECT clock_timestamp()")).scalar()
        try:
            locked = db.execute(text(_LOCK_SQL), {"uid": user_id, "at": at}).fetchone()
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            db.rollback()
            return Outcome(user_id, "skipped_locked", "row held by a charging statement")
        if locked is None:
            db.rollback()
            return Outcome(user_id, "not_a_candidate")

        used = int(locked.records_used or 0)
        guards = db.execute(
            text(_GUARDS_SQL).bindparams(bindparam("terminal", expanding=True)),
            {"uid": user_id, "terminal": list(_TERMINAL),
             "trial_end": locked.trial_ends_at, "start": locked.quota_period_start},
        ).one()

        def refuse(status: str, detail: str) -> Outcome:
            db.rollback()
            return Outcome(user_id, status, detail, used)

        if guards.in_flight:
            return refuse("refused_job_in_flight", f"{guards.in_flight} non-terminal job(s)")
        if guards.malformed:
            # The billing CAS writes both columns together. A count with no
            # timestamp cannot be placed on either side of the trial end, so the
            # ledger comparison below would silently leave it out. (Codex)
            return refuse("refused_malformed_ledger",
                          f"{guards.malformed} job(s) with billed_count but no billing_applied_at")
        if guards.billed_after or guards.reserved_after:
            return refuse(
                "refused_post_trial_usage",
                f"billed {guards.billed_after} / reserved {guards.reserved_after} "
                "at or after the trial end",
            )
        if int(guards.trial_ledger) != used:
            return refuse(
                "refused_counter_mismatch",
                f"counter {used} != ledger billed during trial {guards.trial_ledger}",
            )

        row = db.execute(
            text(_REPAIR_SQL),
            {"uid": user_id, "at": at},
        ).fetchone()
        if row is None:
            return refuse(
                "refused_not_rollable",
                "the corrected window may not roll (frozen, or an entitlement end)",
            )
        if commit:
            db.commit()
        else:
            db.rollback()
        return Outcome(
            user_id, "repaired" if commit else "would_repair",
            f"records_used {used} -> {row.records_used} (limit {row.records_limit})",
            used, row.new_start, row.new_end,
        )
    except Exception:
        db.rollback()
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="apply (default is a dry run)")
    ap.add_argument("--i-understand", action="store_true",
                    help="required with --commit: this rewrites quota windows and counters")
    ap.add_argument("--user-id", default=None, help="restrict to a single user id")
    args = ap.parse_args()
    if args.commit and not args.i_understand:
        print("REFUSING: --commit requires --i-understand")
        return 2

    mode = "COMMIT" if args.commit else "DRY RUN (each statement rolled back)"
    with system_sync_session() as db:
        candidates = find_candidates(db, args.user_id)
        print(f"{mode}: {len(candidates)} candidate(s)")
        refused = 0
        for c in candidates:
            print(f"- {str(c.id)[:8]} plan={c.plan} used={c.records_used}/{c.records_limit} "
                  f"trial_end={c.trial_ends_at.isoformat()} "
                  f"window=[{c.quota_period_start.isoformat()}, {c.quota_period_end.isoformat()})")
            o = repair_user(db, str(c.id), commit=args.commit)
            line = f"    {o.status}: {o.detail}"
            if o.new_period_start is not None:
                line += (f" new window=[{o.new_period_start.isoformat()}, "
                         f"{o.new_period_end.isoformat()})")
            print(line)
            refused += int(o.status not in _SUCCESS)
    print(f"done: {len(candidates) - refused} repairable, {refused} refused/skipped")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
