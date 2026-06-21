"""One-time clear of the King tax-delinquent synthetic party_name placeholder.

King's Socrata tax feed has no owner name and King redacts it from bulk downloads,
so historical rows carry a fabricated `Tax Delinquent — $X owed (Parcel …)` name
that reads as a fake name sitting next to real property/tax data. This nulls it out
(honest "not provided") across all existing King tax_delinquent leads. The real
owner name then comes from skip-trace (address → name + phone + email) or per-parcel
eRealProperty enrichment — never a fabricated value.

Non-destructive: only `party_name` is touched, and ONLY on rows whose party_name is
the exact placeholder shape (is_tax_placeholder_party) within a King/WA/tax_delinquent
config. All other fields (property/mailing/legal/amount/skip-trace) are untouched.
Idempotent: re-running matches nothing once cleared.

Usage:
  python scripts/backfill_clear_king_tax_placeholder_names.py --dry-run
  python scripts/backfill_clear_king_tax_placeholder_names.py [--batch 5000]
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("clear_king_tax_names")

_SELECT = text(
    """
    SELECT r.id, r.party_name
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.id > CAST(:last_id AS uuid)
      AND r.party_name LIKE 'Tax Delinquent%'
      AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
    ORDER BY r.id LIMIT :batch
    """
)


def run(batch: int, dry_run: bool) -> None:
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = cleared = skipped = 0
    while True:
        with SyncSessionLocal() as db:
            db.execute(text("SET statement_timeout=120000"))
            rows = db.execute(_SELECT, {"last_id": last_id, "batch": batch}).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            # Confirm the EXACT placeholder shape in Python (SQL LIKE only prefixes).
            ids = []
            for row in rows:
                scanned += 1
                if is_tax_placeholder_party(row.party_name):
                    ids.append(str(row.id))
                else:
                    skipped += 1
            if ids and not dry_run:
                res = db.execute(
                    text(
                        "UPDATE results SET party_name = NULL "
                        "WHERE id = ANY(CAST(:ids AS uuid[])) "
                        "AND party_name LIKE 'Tax Delinquent%'"
                    ),
                    {"ids": ids},
                )
                db.commit()
                cleared += res.rowcount or 0
            else:
                cleared += len(ids)
            _log.info(
                "%sscanned %d | %s %d | skipped(not placeholder) %d — through %s",
                "[DRY-RUN] " if dry_run else "",
                scanned, "would-clear" if dry_run else "cleared", cleared, skipped, last_id,
            )
    _log.info(
        "%sDONE — scanned %d, %s %d, skipped %d",
        "[DRY-RUN] " if dry_run else "",
        scanned, "would-clear" if dry_run else "cleared", cleared, skipped,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.batch, a.dry_run)
