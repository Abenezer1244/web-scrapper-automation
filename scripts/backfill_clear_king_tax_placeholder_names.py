"""One-time clear of the King tax-delinquent synthetic party_name placeholder.

King's Socrata tax feed has no owner name and King redacts it from bulk downloads,
so historical rows carry a fabricated `Tax Delinquent — $X owed (Parcel …)` name
that reads as a fake name sitting next to real property/tax data. This nulls it out
(honest "not provided") across all existing King tax_delinquent leads. The real
owner name then comes from skip-trace (address → name + phone + email) or per-parcel
eRealProperty enrichment — never a fabricated value.

Non-destructive: only `party_name` is touched, and ONLY on rows whose party_name is
the exact placeholder shape (is_tax_placeholder_party) within a King/WA/tax_delinquent
config. The write itself matches each id to the EXACT party_name string we validated
in Python (`(id, original_party_name)` pairs), so a row whose name changed between the
read and the write — e.g. enrichment swapped in a real owner — is never clobbered. All
other fields (property/mailing/legal/amount/skip-trace) are untouched. Idempotent:
re-running matches nothing once cleared.

One-time, offline backfill: it assumes the table is effectively quiescent for King tax
party_name. New daily scrapes now store `party_name=None` (never the placeholder), so
keyset pagination (`r.id > last_id`) can't miss an eligible row a concurrent scrape
inserts — there are none. Re-run any time; it's idempotent.

Performance: the `results` table is 200k+ rows. We resolve the (small) set of
King/WA/tax_delinquent `job_id`s ONCE from jobs⋈scraper_configs, then keyset-paginate
results filtered by `job_id = ANY(...)` — which rides the indexed FK instead of
re-joining the two metadata tables against the whole results table on every batch
(the join-per-batch is what timed out at 120s and cleared nothing). Each non-empty
batch short-circuits on `LIMIT`; the ordered scan completes well under the 120s
statement_timeout (verified against prod: terminal empty scan ~36s).

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

# Step 1: resolve the King/WA/tax_delinquent job_ids ONCE (small, on the jobs table).
_JOBS_SELECT = text(
    """
    SELECT j.id
    FROM jobs j
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
    """
)

# Step 2: keyset-paginate results INSIDE those jobs via the indexed job_id FK.
# `party_name LIKE 'Tax Delinquent%'` is only a cheap prefilter; the exact placeholder
# shape is confirmed in Python with is_tax_placeholder_party before any write.
_SELECT = text(
    """
    SELECT r.id, r.party_name
    FROM results r
    WHERE r.job_id = ANY(CAST(:job_ids AS uuid[]))
      AND r.id > CAST(:last_id AS uuid)
      AND r.party_name LIKE 'Tax Delinquent%'
    ORDER BY r.id LIMIT :batch
    """
)

# Write guard: null a row only if its party_name is STILL the exact string we
# validated in Python (pair each id with its original value via unnest). This closes
# the read→write gap — if enrichment swapped in a real owner between SELECT and UPDATE,
# the equality fails and the row is left alone.
_UPDATE = text(
    """
    UPDATE results r SET party_name = NULL
    FROM unnest(CAST(:ids AS uuid[]), CAST(:names AS text[])) AS u(id, name)
    WHERE r.id = u.id AND r.party_name = u.name
    """
)


def run(batch: int, dry_run: bool) -> None:
    with SyncSessionLocal() as db:
        db.execute(text("SET statement_timeout=120000"))
        job_ids = [str(r.id) for r in db.execute(_JOBS_SELECT).fetchall()]
    _log.info("resolved %d King/WA/tax_delinquent job_ids", len(job_ids))
    if not job_ids:
        _log.info("no King tax_delinquent jobs — nothing to do")
        return

    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = cleared = skipped = 0
    while True:
        with SyncSessionLocal() as db:
            db.execute(text("SET statement_timeout=120000"))
            rows = db.execute(
                _SELECT, {"job_ids": job_ids, "last_id": last_id, "batch": batch}
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            # Confirm the EXACT placeholder shape in Python (SQL LIKE only prefixes).
            ids: list[str] = []
            names: list[str] = []
            for row in rows:
                scanned += 1
                if is_tax_placeholder_party(row.party_name):
                    ids.append(str(row.id))
                    names.append(row.party_name)
                else:
                    skipped += 1
            if ids and not dry_run:
                res = db.execute(_UPDATE, {"ids": ids, "names": names})
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
    if a.batch < 1:
        ap.error("--batch must be >= 1")
    run(a.batch, a.dry_run)
