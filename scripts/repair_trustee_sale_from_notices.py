"""Repair trustee_sale leads from their SOURCE notice rows (idempotent, dry-run first).

Background (2026-09-02 "Test 3" audit): the Tacoma Daily Index NTS parser lost the
section-IV amount on "matured obligation" notices and truncated a grantor at a
parenthetical title note. Both fixes live in the parser / read-time cleaner, and the
NTS crawler refreshes ``nts_notices`` on its next run — but a delivered ``results``
row is never re-synced from its notice, so already-scraped leads stay wrong.

This script re-derives ONLY from the lead's own notice (``results.nts_notice_id``):

1. ``default_amount``: rows where it is NULL and the notice now carries
   ``principal_owing`` — copied verbatim. Never estimated.
2. ``party_name`` (``--party-names``): trustee_sale rows whose name still ends with
   the truncation signature ``(`` — recomputed with the same read-time cleaner the
   scraper uses (``BridgeScraper.clean(strip_vesting_clause(notice.grantor))``), so a
   repaired row equals what a fresh scrape would produce.

Run the NTS crawler first (so the notice rows are re-parsed with the fixed code),
then:

    railway run --service worker python scripts/repair_trustee_sale_from_notices.py                # dry-run
    railway run --service worker python scripts/repair_trustee_sale_from_notices.py --apply
    railway run --service worker python scripts/repair_trustee_sale_from_notices.py --apply --party-names

Safe to re-run: every write is guarded by the exact condition it repairs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.base_scraper import BridgeScraper  # noqa: E402
from src.scrapers.preforeclosure import strip_vesting_clause  # noqa: E402

# Amount: copy the re-parsed notice amount onto leads that shipped without one.
_AMOUNT_CANDIDATES = text(
    """
    SELECT r.id, r.job_id, n.ts_number, n.principal_owing
    FROM results r
    JOIN nts_notices n ON n.id = CAST(r.nts_notice_id AS uuid)
    WHERE r.default_amount IS NULL AND n.principal_owing IS NOT NULL
    ORDER BY r.created_at
    """
)
_AMOUNT_REPAIR = text(
    """
    UPDATE results r SET default_amount = n.principal_owing
    FROM nts_notices n
    WHERE n.id = CAST(r.nts_notice_id AS uuid)
      AND r.default_amount IS NULL AND n.principal_owing IS NOT NULL
    """
)
# Party name: only the truncation signature the old parser produced.
_NAME_CANDIDATES = text(
    """
    SELECT r.id, r.job_id, r.party_name, n.grantor
    FROM results r
    JOIN nts_notices n ON n.id = CAST(r.nts_notice_id AS uuid)
    WHERE r.party_name ~ '\\(\\s*$' AND n.grantor IS NOT NULL
    ORDER BY r.created_at
    """
)
_NAME_REPAIR = text(
    "UPDATE results SET party_name = :name WHERE id = :rid AND party_name = :old"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument(
        "--party-names", action="store_true",
        help="also repair party_name on rows still carrying the '(' truncation",
    )
    args = ap.parse_args()

    with system_sync_session() as db:
        amount_rows = db.execute(_AMOUNT_CANDIDATES).fetchall()
        print(f"default_amount: {len(amount_rows)} lead(s) whose notice now carries an amount")
        for row in amount_rows:
            print(f"  result={row.id} job={row.job_id} ts={row.ts_number} -> {row.principal_owing}")

        name_plan: list[tuple] = []
        if args.party_names:
            for row in db.execute(_NAME_CANDIDATES).fetchall():
                new = BridgeScraper.clean(strip_vesting_clause(row.grantor))
                if new and new != row.party_name:
                    name_plan.append((row.id, row.party_name, new))
            print(f"party_name: {len(name_plan)} lead(s) to recompute from the notice grantor")
            for rid, old, new in name_plan:
                print(f"  result={rid} {old!r} -> {new!r}")

        if not args.apply:
            print("dry-run: nothing written (re-run with --apply)")
            db.rollback()
            return 0

        amount_written = db.execute(_AMOUNT_REPAIR).rowcount or 0
        name_written = 0
        for rid, old, new in name_plan:
            name_written += db.execute(_NAME_REPAIR, {"rid": rid, "old": old, "name": new}).rowcount or 0
        db.commit()
        print(f"applied: default_amount rows={amount_written}, party_name rows={name_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
