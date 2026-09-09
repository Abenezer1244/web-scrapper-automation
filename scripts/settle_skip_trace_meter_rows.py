"""Settle skip-trace meter rows that are waiting on a human.

Automatic billing of skip-trace overage is off (see
``USAGE_PROVENANCE_IS_TRUSTWORTHY`` in ``src/api/billing/skip_trace_usage.py``):
nothing records when the provider actually ran the lookups, so no stored
timestamp can be defended if a customer disputes the charge. Everything
otherwise billable lands in ``needs_review``.

That destination was, until this script, a dead end — the states existed as
declarations and guards with nothing able to write them. An alert that names a
problem nobody can act on is worse than no alert, because it trains people to
ignore it. Codex found the gap.

Two outcomes, and both are deliberate decisions a person makes and this records:

  settled   the usage was recovered OUTSIDE the meter — an invoice raised by
            hand, a credit note, a negotiated amount. ``--reference`` is
            REQUIRED: a settlement nobody can trace back to an invoice is
            indistinguishable from a write-off six months later.

  writeoff  the usage will not be charged. Also requires a reason, because
            "why did we give this away" is the question that gets asked.

Both stamp the actor. Neither sends anything to Stripe — by the time a row is
here, the honest window for a MeterEvent has closed.

Usage:

    # look first, always
    python scripts/settle_skip_trace_meter_rows.py --list
    python scripts/settle_skip_trace_meter_rows.py --list --user <uuid>

    # then decide, per row or per user
    python scripts/settle_skip_trace_meter_rows.py \\
        --settle <row-id> --reference "INV-1042" --actor "ab"
    python scripts/settle_skip_trace_meter_rows.py \\
        --writeoff-user <uuid> --reason "goodwill, disputed provenance" \\
        --actor "ab"

Nothing is written without ``--yes``; the default is a dry run that prints what
it would do.
"""
import argparse
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_SETTLEABLE = ("needs_review",)

# The one reason a row can be moved BACK out of review rather than settled.
#
# A plan change marks this period's reported usage as stranded BEFORE it asks
# Stripe to replace the metered item, because doing it afterwards was not
# retry-safe. That ordering can only ever over-flag: if Stripe then refuses, the
# old item is still on the subscription, the usage was never stranded, and those
# rows are sitting in review for a transition that did not happen (Codex).
#
# --release exists for exactly that, and ONLY for that reason, so it cannot be
# used to quietly undo a real decision.
_RELEASABLE_REASON = "metered_item_replaced_before_invoice"


def _rows(db, user_id=None, row_id=None):
    where = ["disposition = ANY(:states)"]
    params: dict = {"states": list(_SETTLEABLE)}
    if user_id:
        where.append("user_id = CAST(:uid AS uuid)")
        params["uid"] = user_id
    if row_id:
        where.append("id = CAST(:rid AS uuid)")
        params["rid"] = row_id
    return db.execute(
        text(
            "SELECT id, user_id, billable_units, plan, disposition, "
            "       disposition_reason, created_at "
            "FROM skip_trace_meter_events "
            "WHERE " + " AND ".join(where) + " ORDER BY created_at"
        ),
        params,
    ).fetchall()


def _apply(db, ids, disposition, reason, actor, reference):
    """Apply the decision and return the ids ACTUALLY changed.

    RETURNING, not rowcount-blind. Two operators can read the same review row;
    one commits first, and the second's guarded UPDATE then correctly matches
    nothing. Reporting the count we SELECTED would tell that second operator we
    recorded their write-off and their name against a row that now carries
    somebody else's decision. The caller compares this against what it asked
    for and reports the difference as a conflict.

    Actor and reference go in their own columns. Concatenating them into
    disposition_reason overflowed it, and Postgres answers an over-length value
    by rejecting the statement — the settlement would roll back silently.
    """
    changed = db.execute(
        text(
            "UPDATE skip_trace_meter_events "
            "   SET disposition = :d, "
            "       disposition_at = :now, "
            "       disposition_reason = :reason, "
            "       disposition_actor = :actor, "
            "       disposition_reference = :reference "
            " WHERE id = ANY(CAST(:ids AS uuid[])) "
            # Only ever moves a row OUT of review. A row someone already
            # settled is not re-settled by a second run of this script.
            "   AND disposition = ANY(:states) "
            " RETURNING id"
        ),
        {
            "d": disposition,
            "now": datetime.now(UTC),
            "reason": reason,
            "actor": actor,
            "reference": reference,
            "ids": list(ids),
            "states": list(_SETTLEABLE),
        },
    ).fetchall()
    return [str(r.id) for r in changed]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show rows awaiting review")
    ap.add_argument("--user", help="restrict to one user id")
    ap.add_argument("--settle", metavar="ROW_ID", help="mark one row settled_manual")
    ap.add_argument("--writeoff", metavar="ROW_ID", help="mark one row written_off_manual")
    ap.add_argument("--writeoff-user", metavar="USER_ID", help="write off every review row for a user")
    ap.add_argument("--reference", help="invoice / credit-note reference (required to settle)")
    ap.add_argument("--reason", help="why (required to write off)")
    ap.add_argument("--actor", help="who is deciding this")
    ap.add_argument(
        "--release", metavar="ROW_ID",
        help="put a row wrongly flagged by an ABANDONED plan change back to 'reported' "
             "(only rows whose reason is metered_item_replaced_before_invoice)",
    )
    ap.add_argument(
        "--release-user", metavar="USER_ID",
        help="release every such row for a user",
    )
    ap.add_argument("--yes", action="store_true", help="actually write; default is a dry run")
    a = ap.parse_args()

    with system_sync_session() as db:
        # EVERY action must appear here. --release did not, so it fell into
        # list mode and printed the review queue while releasing nothing, and
        # said so in a way that read like success. Codex found it by running it.
        _ACTIONS = (a.settle, a.writeoff, a.writeoff_user, a.release, a.release_user)
        if a.list or not any(_ACTIONS):
            rows = _rows(db, user_id=a.user)
            if not rows:
                print("Nothing awaiting review.")
                return 0
            total = sum(r.billable_units for r in rows)
            print(f"{len(rows)} row(s) awaiting review, {total} billable unit(s):\n")
            for r in rows:
                print(
                    f"  {r.id}  user={str(r.user_id)[:8]}  units={r.billable_units:>5}"
                    f"  plan={r.plan or '-':<9} reason={r.disposition_reason}"
                    f"  since={r.created_at:%Y-%m-%d}"
                )
            return 0

        # Validated BEFORE the dry run, not after. A dry run that prints "WOULD
        # SET" for a value the real UPDATE will reject teaches the operator to
        # trust a preview that is wrong (Codex). Whitespace is not a name.
        a.actor = (a.actor or "").strip()
        a.reason = (a.reason or "").strip() or None
        a.reference = (a.reference or "").strip() or None

        if not a.actor:
            print("--actor is required: a decision with no name on it is not a decision.")
            return 2
        for field, value in (("--actor", a.actor), ("--reference", a.reference)):
            # varchar(128) in migration 092. Over-length fails the UPDATE, which
            # rolls the whole settlement back after reporting it would succeed.
            if value and len(value) > 128:
                print(f"{field} is {len(value)} characters; the column holds 128.")
                return 2

        # Exactly one action. Accepting several and silently picking a branch
        # means an operator can believe they wrote off one row while the script
        # wrote off a different one.
        chosen = [
            f for f in
            (a.settle, a.writeoff, a.writeoff_user, a.release, a.release_user)
            if f
        ]
        if len(chosen) > 1:
            print("--settle, --writeoff and --writeoff-user are mutually exclusive.")
            return 2

        if a.release or a.release_user:
            # Back to 'reported', which is where these rows were before the
            # abandoned plan change touched them. Scoped to the one reason, so a
            # human's settle or write-off can never be reversed by this.
            # Two whole statements rather than one built by concatenation.
            # They differ by a single predicate, but assembling SQL from pieces
            # is the shape that hides an injection even when every piece here is
            # a literal, and a linter that cannot tell the difference is right
            # to refuse to try.
            _RELEASE_BY_ID = """
                UPDATE skip_trace_meter_events
                   SET disposition = 'reported',
                       disposition_at = :now,
                       disposition_reason = :reason
                 WHERE disposition = 'needs_review'
                   AND disposition_reason = :releasable
                   AND id = ANY(CAST(:ids AS uuid[]))
                RETURNING id
            """
            _RELEASE_BY_USER = """
                UPDATE skip_trace_meter_events
                   SET disposition = 'reported',
                       disposition_at = :now,
                       disposition_reason = :reason
                 WHERE disposition = 'needs_review'
                   AND disposition_reason = :releasable
                   AND user_id = CAST(:uid AS uuid)
                RETURNING id
            """
            params = {
                "now": datetime.now(UTC),
                "reason": f"released after abandoned plan change [by {a.actor}]",
                "releasable": _RELEASABLE_REASON,
            }
            if a.release:
                sql, params = _RELEASE_BY_ID, {**params, "ids": [a.release]}
            else:
                sql, params = _RELEASE_BY_USER, {**params, "uid": a.release_user}
            released = db.execute(text(sql), params).fetchall()
            if not released:
                print(
                    "Nothing to release. Only rows held as "
                    f"{_RELEASABLE_REASON} can be released, and only from "
                    "needs_review."
                )
                return 0
            print(
                f"{'WOULD RELEASE' if not a.yes else 'RELEASING'} "
                f"{len(released)} row(s) back to 'reported':"
            )
            for r in released:
                print(f"  {r.id}")
            if not a.yes:
                db.rollback()
                print(chr(10) + "Dry run. Re-run with --yes to apply.")
                return 0
            db.commit()
            print(chr(10) + f"Done. {len(released)} row(s) released, recorded against {a.actor}.")
            return 0

        if a.settle:
            if not a.reference:
                print("--reference is required to settle: an untraceable settlement "
                      "is a write-off wearing a better word.")
                return 2
            target, disposition, reason = [a.settle], "settled_manual", "recovered outside the meter"
        elif a.writeoff or a.writeoff_user:
            if not a.reason:
                print("--reason is required to write off.")
                return 2
            if a.writeoff:
                target, = [[a.writeoff]]
            else:
                target = [str(r.id) for r in _rows(db, user_id=a.writeoff_user)]
            disposition, reason = "written_off_manual", a.reason

        if not target:
            print("No matching rows awaiting review.")
            return 0

        # `--user` scopes the WRITE, not just `--list`. It read as a safety
        # rail and was not one: the row lookup below ignored it, so
        # `--writeoff <row belonging to B> --user <A>` wrote off B's row while
        # displaying A as the restriction (Codex).
        scope_user = a.user or a.writeoff_user
        found = []
        for rid in target:
            found.extend(_rows(db, user_id=scope_user, row_id=rid))
        if not found:
            print("No matching rows awaiting review for this user (already settled?).")
            return 0

        units = sum(r.billable_units for r in found)
        print(f"{'WOULD SET' if not a.yes else 'SETTING'} {len(found)} row(s) "
              f"({units} unit(s)) to {disposition}:")
        for r in found:
            print(f"  {r.id}  user={str(r.user_id)[:8]}  units={r.billable_units}")

        if not a.yes:
            print("\nDry run. Re-run with --yes to apply.")
            return 0

        asked = [str(r.id) for r in found]
        changed = _apply(db, asked, disposition, reason, a.actor, a.reference)
        db.commit()
        print(f"\nDone. {len(changed)} row(s) -> {disposition}, recorded against {a.actor}.")

        missed = set(asked) - set(changed)
        if missed:
            # Somebody else decided these between our read and our write. Say so
            # loudly: the operator must not walk away believing their decision
            # and their name are on a row that now carries someone else's.
            print(
                f"\nCONFLICT: {len(missed)} row(s) were settled by someone "
                "else while this ran and were NOT changed. Nothing was recorded "
                "against them. Re-run --list to see their current state:"
            )
            for rid in sorted(missed):
                print(f"  {rid}")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
