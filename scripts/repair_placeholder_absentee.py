"""Repair absentee_owner on leads whose property street is a PLACEHOLDER.

The Snohomish tax bulk file encodes "no situs on file" as the literal word
UNKNOWN, so property_address reads 'UNKNOWN UNKNOWN, GRANITE FALLS WA 98252'.
'UNKNOWN UNKNOWN' never equals a real mailing street, so _addresses_differ
returned a CONFIDENT absentee_owner = TRUE for a property whose address we do not
have. Measured in production 2026-09-03: 408 such rows, every one
snohomish / tax_delinquent, all absentee_owner = TRUE.

The code fix (address_intel.street_is_placeholder) makes NEW rows honest; this
repairs the ones already written. User approved the repair 2026-09-03.

Deliberately narrow: only absentee_owner changes. In 'UNKNOWN UNKNOWN, GRANITE
FALLS WA 98252' the LOCALITY is real, so property_state / owner_state /
out_of_state_owner keep their computed values — they do not depend on the street.

Dry-run by default; --apply writes. Every write is a guarded UPDATE (the row must
still hold the value we read), and every row is journalled to a JSONL evidence
file. Nothing is invented: a value we cannot know becomes NULL, never a guess.

    railway run --service worker python scripts/repair_placeholder_absentee.py
    railway run --service worker python scripts/repair_placeholder_absentee.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.utils.address_intel import (  # noqa: E402
    street_is_placeholder,
    compute_owner_flags,
)

_CANDIDATES = text(
    """
    SELECT r.id, r.property_address, r.mailing_address, r.absentee_owner,
           r.out_of_state_owner, r.owner_state, r.property_state,
           sc.county, sc.record_type
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.property_address IS NOT NULL
      AND r.absentee_owner IS NOT NULL
      AND upper(r.property_address) LIKE '%UNKNOWN%'
    ORDER BY sc.county, r.id
    """
)

# Guarded on the SAME fields the decision was made from. `absentee_owner IS TRUE`
# alone was not enough (Codex): the verdict rests on property_address, so if
# enrichment replaced the placeholder situs with a REAL street between this run's
# read and its write, the flag could have become legitimately True and we would
# have cleared it. Re-checking both addresses makes that a rowcount-0 conflict.
_UPDATE = text(
    """
    UPDATE results
    SET absentee_owner = NULL
    WHERE id = :id
      AND absentee_owner IS TRUE
      AND property_address = :property_address
      AND mailing_address IS NOT DISTINCT FROM :mailing_address
    """
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--report", default="placeholder_absentee_repair.jsonl")
    args = ap.parse_args()

    totals = {"candidates": 0, "placeholder": 0, "to_clear": 0, "updated": 0,
              "stale": 0, "left_alone": 0}
    by_county: dict[str, int] = {}

    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        rows = db.execute(_CANDIDATES).fetchall()
        totals["candidates"] = len(rows)

        plans = []
        for r in rows:
            # The SQL LIKE is only a cheap prefilter; the REAL decision uses the
            # same helper the worker uses, so script and worker can never diverge.
            if not street_is_placeholder(r.property_address):
                totals["left_alone"] += 1
                continue
            totals["placeholder"] += 1
            flags = compute_owner_flags(r.property_address, r.mailing_address)
            if flags["absentee_owner"] is not None or r.absentee_owner is not True:
                totals["left_alone"] += 1
                continue
            totals["to_clear"] += 1
            by_county[f"{r.county}/{r.record_type}"] = (
                by_county.get(f"{r.county}/{r.record_type}", 0) + 1
            )
            fh.write(json.dumps({
                "id": str(r.id), "county": r.county, "record_type": r.record_type,
                "property_address": r.property_address,
                "mailing_address": r.mailing_address,
                "absentee_owner": {"was": r.absentee_owner, "now": None},
                # untouched on purpose - these do not depend on the street
                "kept": {"property_state": r.property_state,
                         "owner_state": r.owner_state,
                         "out_of_state_owner": r.out_of_state_owner},
            }, default=str) + "\n")
            plans.append(r)

        if args.apply:
            for r in plans:
                rc = db.execute(_UPDATE, {
                    "id": r.id,
                    "property_address": r.property_address,
                    "mailing_address": r.mailing_address,
                }).rowcount or 0
                totals["updated"] += rc
                totals["stale"] += 1 - rc
                if rc != 1:
                    print(f"  CONFLICT id={r.id} rowcount={rc} — row changed since read, not cleared")
            db.commit()
        else:
            db.rollback()

    print(("applied: " if args.apply else "dry-run: ") + json.dumps(totals))
    print("by county/record_type: " + json.dumps(by_county))
    print(f"evidence -> {args.report}")
    return 0 if totals["stale"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
