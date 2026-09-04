"""Recompute absentee_owner on leads whose stored flag predates a comparator fix.

`absentee_owner` is a stored TRI-STATE derived at scrape time by
`address_intel.compute_owner_flags`. When that comparator is corrected, rows already
written keep the OLD verdict — a confident True that the current code would not make.
This re-runs the SAME `compute_owner_flags` the worker runs over every row currently
claiming absentee, and writes back whatever it now returns. Nothing is invented: a
value the code cannot determine becomes NULL (unknown), never a guess.

Two comparator fixes are known to have left stale rows behind:

  * TRAILING SUFFIX / DIRECTIONAL — a county situs omits the suffix or
    post-directional the owner's mailing address keeps ("21008 SPRINGHAVEN WAY" vs
    "21008 SPRINGHAVEN WAY E"). Same base street, so the honest answer is NULL, not
    absentee. Fixed earlier by `_same_street_modulo_trailing_tokens`.
  * GLUED CITY — a Notice of Trustee Sale prints the situs with no comma before the
    city ("1207 118TH PL SW EVERETT, WASHINGTON 98204-4813"), so the comma-splitting
    parser reads the city as part of the STREET. That shape only survived via the
    whole-string equality shortcut, which breaks when one side carries ZIP+4 and the
    other ZIP5 — a confident absentee=True for an owner living in the house. Fixed by
    `_street_absorbed_city`.

Measured in production 2026-09-03 over 2,432 absentee rows: 17 stale — 16 True→NULL
from the suffix case (king/pierce probate + pre_foreclosure) and 1 True→False from
the glued-city case (a Snohomish trustee-sale lead).

Deliberately narrow: only absentee_owner changes. property_state / owner_state /
out_of_state_owner derive from the two STATES, which neither bug affected, so they
keep their computed values.

Dry-run by default; --apply writes. Every write is guarded on the two addresses the
verdict was computed from, so a row whose addresses changed under us is a rowcount-0
conflict rather than a silent overwrite. Every change is journalled to JSONL.

    railway run --service worker python scripts/repair_stale_absentee_owner.py
    railway run --service worker python scripts/repair_stale_absentee_owner.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.utils.address_intel import compute_owner_flags  # noqa: E402

# Cheap prefilter only — the real decision is compute_owner_flags below. Scoped to
# rows currently claiming absentee: both comparator bugs could only ever manufacture
# a spurious TRUE (two identical places made to look different), never a false NULL.
_CANDIDATES = text(
    """
    SELECT r.id, r.property_address, r.mailing_address, r.absentee_owner,
           r.out_of_state_owner, r.owner_state, r.property_state,
           r.property_city, r.property_zip, r.party_name,
           sc.county, sc.record_type
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.absentee_owner IS TRUE
      AND r.property_address IS NOT NULL AND r.property_address <> ''
      AND r.mailing_address  IS NOT NULL AND r.mailing_address  <> ''
    ORDER BY sc.county, r.id
    """
)

# Guarded on the SAME fields the verdict was computed from: if enrichment rewrote
# either address between this run's read and its write, the row's absentee value
# may now be legitimately True and must not be clobbered.
_UPDATE = text(
    """
    UPDATE results
    SET absentee_owner = :absentee
    WHERE id = :id
      AND absentee_owner IS TRUE
      AND property_address = :property_address
      AND mailing_address = :mailing_address
    """
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--report", default="stale_absentee_repair.jsonl")
    args = ap.parse_args()

    totals = {"candidates": 0, "to_change": 0, "updated": 0, "stale": 0, "left_alone": 0}
    by_source: dict[str, int] = {}

    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        rows = db.execute(_CANDIDATES).fetchall()
        totals["candidates"] = len(rows)

        plans = []
        for r in rows:
            # Recompute with the CURRENT code, passing the structured situs exactly
            # as the worker does (migration 085) so the two can never disagree.
            flags = compute_owner_flags(
                r.property_address, r.mailing_address,
                property_city=r.property_city,
                property_state=r.property_state,
                property_zip=r.property_zip,
            )
            now = flags["absentee_owner"]
            if now is True:
                totals["left_alone"] += 1
                continue
            totals["to_change"] += 1
            key = f"{r.county}/{r.record_type}"
            by_source[key] = by_source.get(key, 0) + 1
            fh.write(json.dumps({
                "id": str(r.id), "county": r.county, "record_type": r.record_type,
                "party_name": r.party_name,
                "property_address": r.property_address,
                "mailing_address": r.mailing_address,
                "absentee_owner": {"was": r.absentee_owner, "now": now},
                # untouched on purpose — these derive from the two STATES, which the
                # neither bug affected
                "kept": {"property_state": r.property_state,
                         "owner_state": r.owner_state,
                         "out_of_state_owner": r.out_of_state_owner},
            }, default=str) + "\n")
            plans.append((r, now))
            print(f"  {(r.party_name or '')[:30]:32} {r.county}/{r.record_type}")
            print(f"      property = {r.property_address!r}")
            print(f"      mailing  = {r.mailing_address!r}")
            print(f"      absentee_owner: True -> {now!r}")

        if args.apply:
            for r, now in plans:
                rc = db.execute(_UPDATE, {
                    "id": r.id, "absentee": now,
                    "property_address": r.property_address,
                    "mailing_address": r.mailing_address,
                }).rowcount
                if rc == 1:
                    totals["updated"] += 1
                else:
                    # The row moved under us — count it, do not retry blindly.
                    totals["stale"] += 1
            db.commit()
        else:
            db.rollback()

    print(f"\ncandidates={totals['candidates']} to_change={totals['to_change']} "
          f"left_alone={totals['left_alone']}")
    if by_source:
        print(f"  by source: {by_source}")
    if args.apply:
        print(f"APPLIED — updated={totals['updated']} stale={totals['stale']}")
    else:
        print(f"DRY RUN — {totals['to_change']} row(s) would change. Re-run with --apply.")
    print(f"evidence: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
