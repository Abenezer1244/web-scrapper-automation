"""Backfill real owner names onto existing King tax-delinquent leads (PR #80 reach).

PR #80 made King tax_delinquent leads show the real owner (parsed from
eRealProperty) instead of the placeholder party_name
"Tax Delinquent — $X owed (Parcel …)". The forward swap only reaches rows a
fresh job re-enriches, but King tax is a point-in-time snapshot: every parcel
already exists, so existing leads keep the placeholder in the UI. This one-time
(re-runnable) backfill repairs them.

Run MANUALLY — never on API boot (the results table is large). Idempotent +
re-runnable: only rows that are STILL a placeholder are touched, so a re-run
never clobbers a real owner already written.

SCOPE (belt + suspenders, per Codex): a candidate row must satisfy BOTH
  1. party_name is the exact King tax placeholder shape (is_tax_placeholder_party), AND
  2. it belongs to a King / WA / tax_delinquent scraper_config (jobs -> scraper_configs join),
and have a non-NULL parcel_id. The config join — not the placeholder string — is
the authoritative population boundary; the placeholder shape alone is a weak id.

MULTI-TENANT: owner is PUBLIC, parcel-keyed assessor data (identical for every
tenant), so a global parcel->owner cache is not a cross-tenant PII concern. Tenancy
is preserved because the UPDATE only ever touches rows already inside the joined
King-tax population; each row keeps its own user_id (never used as a lookup key).
The worker/system DB session is not RLS-bound, so this SQL scope IS the boundary.

Owner lookup is HTTP-only (batch_extract_king_owners — no Playwright) and cached
across the whole run, so each distinct parcel is fetched at most once even when
many tenant/job rows share it.

Usage:
  python scripts/backfill_king_tax_owner_names.py --dry-run        # preview, no writes
  python scripts/backfill_king_tax_owner_names.py                  # apply
  python scripts/backfill_king_tax_owner_names.py --batch 2000 --limit 500
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners
from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_king_tax_owner_names")

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_SELECT_SQL = text(
    """
    SELECT r.id, r.parcel_id, r.party_name
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.id > CAST(:last_id AS uuid)
      AND r.parcel_id IS NOT NULL
      AND r.party_name LIKE 'Tax Delinquent%'
      AND lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
    ORDER BY r.id
    LIMIT :batch
    """
)


def _resolve_owners(parcels: list[str], cache: dict[str, str | None]) -> None:
    """Fetch owners for parcels not already in `cache`; record misses as None so a
    not-found parcel is never re-fetched within this run."""
    todo = sorted({p for p in parcels if p not in cache})
    if not todo:
        return
    found = asyncio.run(batch_extract_king_owners(todo))
    for pid in todo:
        cache[pid] = found.get(pid)  # real owner or None (looked-up, absent)


def run(batch: int, limit: int | None, dry_run: bool) -> None:
    last_id = _NIL_UUID
    owner_cache: dict[str, str | None] = {}
    scanned = updated = skipped_no_owner = skipped_not_placeholder = 0

    while True:
        # Apply any remaining --limit to the SELECT itself so a bounded test run
        # never scans/updates a full `batch` past the limit.
        effective_batch = batch
        if limit is not None:
            remaining = limit - scanned
            if remaining <= 0:
                _log.info("reached --limit %d, stopping", limit)
                break
            effective_batch = min(batch, remaining)
        with SyncSessionLocal() as db:
            rows = db.execute(
                _SELECT_SQL, {"last_id": last_id, "batch": effective_batch}
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)

            # Confirm the EXACT placeholder shape in Python (the SQL LIKE only
            # narrows on the prefix; is_tax_placeholder_party requires the full
            # "$amount owed (Parcel …)" form).
            candidates = []
            for row in rows:
                scanned += 1
                if is_tax_placeholder_party(row.party_name):
                    candidates.append(row)
                else:
                    skipped_not_placeholder += 1

            if candidates:
                _resolve_owners(
                    [c.parcel_id.strip() for c in candidates], owner_cache
                )
                # (id, owner, old_placeholder) for rows whose parcel resolved to a
                # real owner. old_placeholder re-asserts the still-placeholder guard
                # at write time (idempotent; a concurrent change can't be clobbered).
                triples: list[tuple[str, str, str]] = []
                for c in candidates:
                    owner = owner_cache.get(c.parcel_id.strip())
                    if owner:
                        triples.append((str(c.id), owner, c.party_name))
                    else:
                        skipped_no_owner += 1

                if triples and not dry_run:
                    values_sql = ",".join(
                        f"(:id_{i}, :owner_{i}, :old_{i})" for i in range(len(triples))
                    )
                    params: dict = {}
                    for i, (rid, owner, old) in enumerate(triples):
                        params[f"id_{i}"] = rid
                        params[f"owner_{i}"] = owner
                        params[f"old_{i}"] = old
                    res = db.execute(
                        text(
                            f"""
                            UPDATE results
                            SET party_name = data.owner
                            FROM (VALUES {values_sql}) AS data(id, owner, old_name)
                            WHERE results.id = data.id::uuid
                              AND results.party_name = data.old_name
                            """
                        ),
                        params,
                    )
                    db.commit()
                    # Actual persisted count — the still-placeholder WHERE guard may
                    # be < len(triples) if a row changed since it was read.
                    updated += res.rowcount or 0
                else:
                    # Dry-run reports would-ATTEMPT (the real run's guard may write
                    # fewer); never conflate with a persisted rowcount.
                    updated += len(triples)

            _log.info(
                "%sscanned %d | %s %d | no-owner %d | not-placeholder %d — through id %s",
                "[DRY-RUN] " if dry_run else "",
                scanned,
                "would-attempt" if dry_run else "updated",
                updated, skipped_no_owner, skipped_not_placeholder, last_id,
            )

    _log.info(
        "%sDONE — scanned %d, %s %d, skipped(no owner) %d, skipped(not placeholder) %d, "
        "distinct parcels looked up %d. NOTE: 'no owner' may include transient lookup "
        "failures (see WARNING lines above); this script is idempotent — re-run to retry them.",
        "[DRY-RUN] " if dry_run else "",
        scanned,
        "would-attempt" if dry_run else "updated",
        updated, skipped_no_owner, skipped_not_placeholder, len(owner_cache),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000, help="rows scanned per DB page")
    ap.add_argument("--limit", type=int, default=None, help="stop after scanning N rows (for a bounded test run)")
    ap.add_argument("--dry-run", action="store_true", help="resolve owners + count would-be updates, write nothing")
    args = ap.parse_args()
    run(args.batch, args.limit, args.dry_run)
