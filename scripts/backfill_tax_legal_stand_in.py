"""One-time clear of document-id / parcel stand-ins sitting in `legal_description`
on historical tax_delinquent rows (Snohomish, Clark).

PR #87 fixed every connector going FORWARD so `legal_description` holds a real
property legal description (or None when none is available), with any document/parcel
identifier moved to `enrichment_data`. But the rows scraped BEFORE that merge still
carry the stand-in:

- Snohomish tax bulk file has no legal at all → old rows stored the PARCEL NUMBER in
  `legal_description` (== `parcel_id`). The current scraper sets None; this nulls the
  pre-fix rows so the field is honestly empty (parcel_id keeps its own column).
- Clark tax-lien recorder rows stored the numeric RECORDING/DOCUMENT NUMBER as the
  legal stand-in (e.g. "6309661"). Real Clark legals carry LOT/SUB/PID text and are
  never purely numeric. To null ONLY provably-recoverable rows, the predicate requires
  the all-digits `legal_description` to literally equal the stored
  `enrichment_data.recording_number` — so the value being cleared is guaranteed to
  still live in enrichment_data, not just assumed to (Codex P2).

Non-destructive & recoverable: only `legal_description` is touched, only on rows that
match the county's exact stand-in predicate (confirmed in Python before any write),
and the value being nulled is still available elsewhere (parcel_id column / enrichment).
`results.dedup_hash` excludes `legal_description`, so billing/lead dedup is unaffected.

Performance: same shape as the King clear script — resolve the (small) set of
{county}/tax_delinquent `job_id`s ONCE, then keyset-paginate `results` filtered by
`job_id = ANY(...)` (indexed FK) instead of a per-batch 3-table JOIN.

The write nulls a row only if its `legal_description` is STILL the exact string we
validated (`(id, original_legal)` pairs via unnest), so a concurrent re-scrape that
swapped in a real legal can never be clobbered. Idempotent.

Usage:
  python scripts/backfill_tax_legal_stand_in.py --county snohomish --dry-run
  python scripts/backfill_tax_legal_stand_in.py --county clark [--batch 5000]
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import SyncSessionLocal

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("clear_tax_legal")

# Per-county stand-in definition. `sql_prefilter` is a cheap, index-friendly SELECT
# filter; `is_stand_in(row)` is the authoritative Python confirmation run before any
# write (mirrors the prefilter, never looser).
_COUNTIES = {
    # Snohomish: legal_description literally equals the parcel number.
    "snohomish": {
        "sql_prefilter": "r.legal_description = r.parcel_id",
        "is_stand_in": lambda r: r.legal_description is not None
        and r.legal_description == r.parcel_id,
    },
    # Clark: legal_description is a purely-numeric recording/document number AND it
    # equals the recording_number kept in enrichment_data (recoverable evidence, not
    # just a numeric-shape assumption).
    "clark": {
        "sql_prefilter": "r.legal_description ~ '^[0-9]+$' "
        "AND r.legal_description = r.enrichment_data->>'recording_number'",
        "is_stand_in": lambda r: (r.legal_description or "").isdigit()
        and r.legal_description == r.rec_num,
    },
}

_JOBS_SELECT = text(
    """
    SELECT j.id
    FROM jobs j
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = :county AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
    """
)

_UPDATE = text(
    """
    UPDATE results r SET legal_description = NULL
    FROM unnest(CAST(:ids AS uuid[]), CAST(:legals AS text[])) AS u(id, legal)
    WHERE r.id = u.id AND r.legal_description = u.legal
    """
)


def run(county: str, batch: int, dry_run: bool) -> None:
    cfg = _COUNTIES[county]
    select = text(
        f"""
        SELECT r.id, r.parcel_id, r.legal_description,
               r.enrichment_data->>'recording_number' AS rec_num
        FROM results r
        WHERE r.job_id = ANY(CAST(:job_ids AS uuid[]))
          AND r.id > CAST(:last_id AS uuid)
          AND {cfg['sql_prefilter']}
        ORDER BY r.id LIMIT :batch
        """  # noqa: S608 — prefilter is a fixed per-county literal, not user input
    )

    with SyncSessionLocal() as db:
        db.execute(text("SET statement_timeout=120000"))
        job_ids = [str(r.id) for r in db.execute(_JOBS_SELECT, {"county": county}).fetchall()]
    _log.info("resolved %d %s/WA/tax_delinquent job_ids", len(job_ids), county)
    if not job_ids:
        _log.info("no %s tax_delinquent jobs — nothing to do", county)
        return

    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = cleared = skipped = 0
    while True:
        with SyncSessionLocal() as db:
            db.execute(text("SET statement_timeout=120000"))
            rows = db.execute(
                select, {"job_ids": job_ids, "last_id": last_id, "batch": batch}
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            ids: list[str] = []
            legals: list[str] = []
            for row in rows:
                scanned += 1
                if cfg["is_stand_in"](row):
                    ids.append(str(row.id))
                    legals.append(row.legal_description)
                else:
                    skipped += 1
            if ids and not dry_run:
                res = db.execute(_UPDATE, {"ids": ids, "legals": legals})
                db.commit()
                cleared += res.rowcount or 0
            else:
                cleared += len(ids)
            _log.info(
                "%sscanned %d | %s %d | skipped(not stand-in) %d — through %s",
                "[DRY-RUN] " if dry_run else "",
                scanned, "would-clear" if dry_run else "cleared", cleared, skipped, last_id,
            )
    _log.info(
        "%sDONE [%s] — scanned %d, %s %d, skipped %d",
        "[DRY-RUN] " if dry_run else "",
        county, scanned, "would-clear" if dry_run else "cleared", cleared, skipped,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True, choices=sorted(_COUNTIES))
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.batch < 1:
        ap.error("--batch must be >= 1")
    run(a.county, a.batch, a.dry_run)
