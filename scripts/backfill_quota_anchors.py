"""Move existing subscribers onto their real Stripe entitlement anniversary.

WHY THIS IS A SCRIPT AND NOT PART OF THE MIGRATION
--------------------------------------------------
Migration 088 backfills every user to ``quota_anchor_at = records_period_start``,
which is always the first of a month — i.e. exactly the calendar behaviour they
already had. That is deliberate: the deploy must not move anyone's reset date,
grant a bucket or take one away, and a migration cannot call Stripe anyway.

This script is the SECOND, separate step. It reads each subscriber's
``billing_cycle_anchor`` from Stripe and writes it to ``quota_anchor_at`` —
and NOTHING else. The live window is left exactly where it is, so nobody's quota
changes at the moment this runs. Each user's grid shifts at their own next
natural rollover, and ``quota_transitional_end`` bounds that one-off shift to
within about a fortnight of a month in either direction.

RUN IT ONLY AFTER the entitlement deploy is verified in production, because
until the legacy calendar reset is retired a non-day-1 anchor would be reset
twice: once on the user's own boundary and again on the 1st.

WHY billing_cycle_anchor
------------------------
It is Stripe's STABLE recurring anchor: it survives plan changes, and for an
annual price it is the annual anniversary — whose day-of-month is exactly what
should drive a MONTHLY entitlement grid. ``current_period_start`` moves with
every invoice and would drift.

SAFETY
------
* Dry-run by default. ``--commit`` additionally requires ``--i-understand``.
* Writes ONE column: ``users.quota_anchor_at``. Never touches ``records_used``,
  the live window, the plan, the limit, or any Stripe state.
* Skips any user with no ``stripe_subscription_id`` — Starter, free and
  admin-granted accounts keep the anchor they were migrated with, which is
  correct: they have no subscription anniversary to follow.
* Skips (never guesses) on any Stripe error or a subscription with no
  ``billing_cycle_anchor``. A transient Stripe failure must not re-anchor
  anybody, and ``start_date`` is NOT a substitute for the recurring anchor.
* **Skips a FUTURE ``billing_cycle_anchor``.** The grid only walks forward, so
  an anchor beyond the current window's end stretches the transitional window to
  reach it — measured: anchor 2027-01-20 against a window ending 2026-10-01
  gives a **111-day** window on one month's quota, against 19 days for a past
  anchor. Stripe sets a future anchor legitimately (scheduled cycle change,
  unconverted trial), so the row is left for the live conversion path.
* Each write is guarded on **both** the anchor value AND the subscription id we
  measured, so a concurrent conversion, resubscribe **or cancellation** is
  skipped and reported rather than clobbered. (Cancellation clears the
  subscription id but preserves the anchor, so an anchor-only guard would still
  have matched.)
* Idempotent: a second run finds the anchors already equal and does nothing.
* ⚠️ It re-anchors ANY subscriber whose stored anchor differs from Stripe's,
  including one set earlier by a conversion or by admin action. That is intended
  for the one-off cutover; if you re-run it later, scope it with ``--user-id``
  rather than letting it sweep accounts that were anchored on purpose.

USAGE
-----
    railway run python scripts/backfill_quota_anchors.py
    railway run python scripts/backfill_quota_anchors.py --commit --i-understand
    ... [--user-id <uuid>] to scope to a single account
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

# Run directly (`railway run python scripts/...`) as the docstring documents,
# without needing PYTHONPATH set: put the repo root on the path first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.config import settings  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402

_CANDIDATES_SQL = """
    SELECT id                  AS user_id,
           plan                AS plan,
           subscription_status AS status,
           stripe_subscription_id AS subscription_id,
           quota_anchor_at     AS anchor,
           quota_period_start  AS window_start,
           quota_period_end    AS window_end
    FROM users
    WHERE stripe_subscription_id IS NOT NULL
      {user_filter_and}
    ORDER BY id
"""

#: Guarded on BOTH the anchor and the subscription id we measured. A concurrent
#: trial->paid conversion or resubscribe legitimately re-anchors the user; this
#: must lose that race, not win it.
#:
#: The subscription-id half matters on its own: ``end_subscription()`` CLEARS
#: ``stripe_subscription_id`` but deliberately PRESERVES the anchor, so a
#: cancellation landing between the SELECT and this UPDATE leaves the anchor
#: unchanged — the anchor-only guard would still match and write a now-Starter
#: account's anchor from the subscription they just cancelled. (Codex)
_UPDATE_SQL = """
    UPDATE users
    SET quota_anchor_at = CAST(:new_anchor AS timestamptz)
    WHERE id = CAST(:uid AS uuid)
      AND quota_anchor_at = CAST(:expected AS timestamptz)
      AND stripe_subscription_id = :expected_sub
"""


def _stripe_anchor(subscription_id: str) -> datetime | None:
    """The subscription's stable recurring anchor, or None if Stripe has none.

    ``billing_cycle_anchor`` ONLY. An earlier revision fell back to
    ``start_date``, which contradicts this script's own safety contract
    ("Skips (never guesses)"): the two are not interchangeable, and writing a
    start date as though it were the recurring anchor silently moves the
    customer's reset day to whatever date they happened to sign up on. If
    Stripe has no billing anchor, that user is skipped and reported. (Codex)
    """
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    sub = stripe.Subscription.retrieve(subscription_id)
    raw = sub.get("billing_cycle_anchor")
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw), tz=UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write (requires --i-understand)")
    parser.add_argument("--i-understand", action="store_true",
                        help="acknowledge that this shifts customers' reset dates")
    parser.add_argument("--user-id", default=None,
                        help="scope to a single account")
    args = parser.parse_args()

    if args.commit and not args.i_understand:
        raise SystemExit(
            "REFUSING: --commit requires --i-understand. This shifts every "
            "subscriber's quota reset date once. Confirm the entitlement deploy "
            "is live and the legacy calendar reset is retired first — otherwise "
            "a non-day-1 anchor is reset twice, on its own boundary AND the 1st."
        )

    sql = _CANDIDATES_SQL.format(
        user_filter_and="AND id = CAST(:uid AS uuid)" if args.user_id else ""
    )
    params = {"uid": args.user_id} if args.user_id else {}

    planned: list[tuple] = []
    skipped: list[tuple] = []
    # One clock reading for the whole run, so the future-anchor test cannot give
    # different answers to two rows in the same pass.
    now = datetime.now(UTC)

    with system_sync_session() as db:
        rows = db.execute(text(sql), params).mappings().all()
        print(f"{len(rows)} subscriber(s) with a Stripe subscription id\n")

        for row in rows:
            try:
                anchor = _stripe_anchor(row["subscription_id"])
            except Exception as exc:  # noqa: BLE001 — never re-anchor on a guess
                skipped.append((row["user_id"], f"Stripe error: {str(exc)[:120]}"))
                continue
            if anchor is None:
                skipped.append((row["user_id"], "Stripe has no billing anchor"))
                continue
            # A FUTURE billing_cycle_anchor must never be written here.
            #
            # The grid only walks forward from the anchor, so an anchor after
            # the current window's end makes the transitional window stretch to
            # reach it: measured with the real helpers, anchor 2027-01-20 against
            # a window ending 2026-10-01 yields [2026-10-01 -> 2027-01-20) — a
            # 111-DAY window carrying ONE month's quota, versus 19 days for a
            # past anchor. That hands ~3.7 months of allowance for one payment
            # and breaks both the "always exactly one month" invariant and the
            # ~15-day transitional bound.
            #
            # Stripe sets a future anchor legitimately (a scheduled cycle change,
            # a trial that has not converted yet), so this is not corrupt data —
            # it is data this script is not entitled to act on. The live
            # conversion path handles it deliberately; a bulk backfill has no
            # business guessing. Skip and report; re-run once it is in the past. (Codex)
            if anchor > now:
                skipped.append(
                    (
                        row["user_id"],
                        f"billing_cycle_anchor is in the FUTURE ({anchor}) — would "
                        "stretch the transitional window; left for the conversion path",
                    )
                )
                continue
            current = row["anchor"]
            if current is not None and current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            if current == anchor:
                skipped.append((row["user_id"], "already anchored"))
                continue
            planned.append(
                (row["user_id"], current, anchor, row["window_end"], row["subscription_id"])
            )

        for user_id, current, anchor, window_end, _sub in planned:
            print(
                f"  {user_id}  anchor {current} -> {anchor}   "
                f"(current window ends {window_end}; the grid shifts at that "
                f"rollover, not now)"
            )
        for user_id, reason in skipped:
            print(f"  SKIP {user_id}: {reason}")

        if not args.commit:
            print(
                f"\nDRY RUN — {len(planned)} anchor(s) would move, "
                f"{len(skipped)} skipped. Re-run with --commit --i-understand."
            )
            return 0

        applied = raced = 0
        for user_id, current, anchor, _end, sub_id in planned:
            rowcount = db.execute(
                text(_UPDATE_SQL),
                {
                    "uid": str(user_id),
                    "new_anchor": anchor,
                    "expected": current,
                    "expected_sub": sub_id,
                },
            ).rowcount
            if rowcount == 1:
                applied += 1
            else:
                raced += 1
                print(
                    f"  RACED {user_id}: the anchor OR the subscription id changed "
                    "under us between the read and the write — a concurrent "
                    "conversion re-anchors legitimately, and a cancellation clears "
                    "the subscription while keeping the anchor. Left alone; re-run "
                    "to pick it up if it still qualifies."
                )
        db.commit()
        print(f"\nAPPLIED {applied} anchor(s); {raced} raced; {len(skipped)} skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
