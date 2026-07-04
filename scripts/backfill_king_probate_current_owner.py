"""Backfill the Assessor CURRENT owner + title_status onto existing King probate /
death-certificate leads (feat/probate-current-owner).

A probate lead's party_name is the DECEASED. The King Assessor's owner is who holds
title NOW — often an heir or a trust. The forward enrichment (enrich.py) only reaches
rows a fresh job re-enriches, so existing leads never show it. This one-time
(re-runnable) backfill fills enrichment_data.assessor_current_owner +
enrichment_data.title_status for existing King probate/death rows.

Run MANUALLY — never on API boot (the results table is large). Idempotent: only rows
that do NOT yet carry assessor_current_owner are touched, so a re-run never clobbers a
value already written and safely retries transient lookup misses.

SCOPE (belt + suspenders): a candidate row must (1) belong to a King / WA /
{probate|death_certificate} scraper_config (jobs -> scraper_configs join — the
authoritative population boundary), (2) have a non-NULL parcel_id, and (3) not yet
carry assessor_current_owner. NEVER overwrites party_name; NEVER drops a lead.

MULTI-TENANT: owner is PUBLIC parcel-keyed assessor data (same for every tenant), so a
global parcel->owner cache is not a cross-tenant PII concern. The UPDATE only touches
rows already inside the joined King-probate population; each row keeps its own user_id.
The worker/system DB session is not RLS-bound, so this SQL scope IS the boundary.

Owner lookup is HTTP-only (batch_extract_king_owners — no Playwright), cached across the
whole run, so each distinct parcel is fetched at most once.

Usage:
  python scripts/backfill_king_probate_current_owner.py --dry-run
  python scripts/backfill_king_probate_current_owner.py
  python scripts/backfill_king_probate_current_owner.py --batch 2000 --limit 500
"""
import argparse
import asyncio
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners
from src.utils.lead_formatting import classify_probate_title_status

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_king_probate_current_owner")

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_SELECT_SQL = text(
    """
    SELECT r.id, r.parcel_id, r.party_name
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.id > CAST(:last_id AS uuid)
      AND r.parcel_id IS NOT NULL
      AND lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
      AND sc.record_type IN ('probate', 'death_certificate')
      AND (r.enrichment_data->>'assessor_current_owner') IS NULL
    ORDER BY r.id
    LIMIT :batch
    """
)

# jsonb-merge the two new keys; idempotent guard re-asserts assessor_current_owner is
# still unset at write time so a concurrent enrichment can't be clobbered.
_UPDATE_TMPL = """
UPDATE results
SET enrichment_data = COALESCE(results.enrichment_data::jsonb, '{{}}'::jsonb)
    || jsonb_build_object('assessor_current_owner', data.owner, 'title_status', data.status)
FROM (VALUES {values_sql}) AS data(id, owner, status)
WHERE results.id = data.id::uuid
  AND (results.enrichment_data->>'assessor_current_owner') IS NULL
"""


def _resolve_owners(parcels: list[str], cache: dict[str, str | None], delay: float) -> None:
    """Fetch owners for parcels not already in `cache`; record misses as None so a
    not-found parcel is never re-fetched within this run."""
    todo = sorted({p for p in parcels if p not in cache})
    if not todo:
        return
    found = asyncio.run(batch_extract_king_owners(todo, delay=delay))
    for pid in todo:
        cache[pid] = found.get(pid)


def run(batch: int, limit: int | None, dry_run: bool, delay: float) -> None:
    last_id = _NIL_UUID
    owner_cache: dict[str, str | None] = {}
    scanned = updated = skipped_no_owner = 0
    flagged = {"current_owner_name_differs": 0, "current_owner_entity_or_trust": 0, "": 0}

    while True:
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
            scanned += len(rows)

            _resolve_owners([r.parcel_id.strip() for r in rows], owner_cache, delay)
            # (id, owner, title_status) for rows whose parcel resolved to a real owner.
            triples: list[tuple[str, str, str]] = []
            for row in rows:
                owner = owner_cache.get(row.parcel_id.strip())
                if not owner:
                    skipped_no_owner += 1
                    continue
                status = classify_probate_title_status(row.party_name, owner)
                flagged[status] = flagged.get(status, 0) + 1
                triples.append((str(row.id), owner, status))

            if triples and not dry_run:
                values_sql = ",".join(
                    f"(:id_{i}, :owner_{i}, :status_{i})" for i in range(len(triples))
                )
                params: dict = {}
                for i, (rid, owner, status) in enumerate(triples):
                    params[f"id_{i}"] = rid
                    params[f"owner_{i}"] = owner
                    params[f"status_{i}"] = status
                res = db.execute(text(_UPDATE_TMPL.format(values_sql=values_sql)), params)
                db.commit()
                updated += res.rowcount or 0
            else:
                updated += len(triples)

            _log.info(
                "%sscanned %d | %s %d | no-owner %d | differs %d | trust/entity %d — through id %s",
                "[DRY-RUN] " if dry_run else "",
                scanned,
                "would-attempt" if dry_run else "updated",
                updated, skipped_no_owner,
                flagged["current_owner_name_differs"], flagged["current_owner_entity_or_trust"],
                last_id,
            )

    _log.info(
        "%sDONE — scanned %d, %s %d, no-owner %d, distinct parcels %d | "
        "flags: differs=%d entity/trust=%d same/blank=%d. 'no owner' may include "
        "transient failures; idempotent — re-run to retry.",
        "[DRY-RUN] " if dry_run else "",
        scanned,
        "would-attempt" if dry_run else "updated",
        updated, skipped_no_owner, len(owner_cache),
        flagged["current_owner_name_differs"], flagged["current_owner_entity_or_trust"],
        flagged.get("", 0),
    )


def _polite_delay(raw: str) -> float:
    """argparse type for --delay: finite, non-negative, floored at 0.1s (polite)."""
    v = float(raw)
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"--delay must be finite, non-negative (got {raw!r})")
    return max(v, 0.1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000, help="rows scanned per DB page")
    ap.add_argument("--limit", type=int, default=None, help="stop after scanning N rows")
    ap.add_argument("--dry-run", action="store_true", help="resolve + count, write nothing")
    ap.add_argument(
        "--delay", type=_polite_delay, default=0.6,
        help="seconds between eRealProperty requests (default 0.6, min 0.1)",
    )
    args = ap.parse_args()
    run(args.batch, args.limit, args.dry_run, args.delay)
