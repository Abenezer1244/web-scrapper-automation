"""Queue historical leads for county-GIS mailing recovery.

Background (2026-09-10): Snohomish and Cowlitz had no mailing-address source at all.
The worker's GIS batch knew only Pierce and King, so every other WA county fell to the
statewide situs-only layer and stored a property address with a structurally NULL
mailing address (testserserf: Cowlitz probate 0/22; Snohomish pre_foreclosure 0/7).
Both counties now have a source, which fixes NEW jobs. This fixes the leads that
already exist, without a re-scrape.

What it does, and all it does: put the existing `mailing_lookup_deferred` marker on
eligible rows. It never looks anything up and never writes a mailing address. The
background sweep (`src/workers/mailing_recovery.recover_deferred_gis_mailing`, beat
every 10 minutes, 200 parcels per tick) then performs the lookup under its own guards:
terminal jobs only, fill-only, tenant + parcel pinned in the UPDATE, attempt ceiling,
owner flags recomputed from the written address, and no billing, quota, job creation or
skip trace. So a lead keeps its id, job, property address, phones and emails.

Eligible row: county in --counties (must have a GIS mailing source), WA, job status
done, mailing_address NULL, parcel_id of at least 6 characters, not already deferred,
and never concluded by recovery (no `mailing_recovery_outcome`). A row recovery already
answered "none" is not re-queued.

    railway run --service worker python scripts/requeue_gis_mailing_recovery.py            # dry-run
    railway run --service worker python scripts/requeue_gis_mailing_recovery.py --apply

Every candidate is written to --report (JSON lines) before any write.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

DEFAULT_COUNTIES = ("snohomish", "cowlitz")
REASON = "county_gis_mailing_source_added"

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.job_id, r.parcel_id, lower(sc.county) AS county,
           r.is_duplicate, r.property_address IS NOT NULL AS has_property
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = ANY(:counties)
      AND upper(sc.state) = 'WA'
      AND j.status = 'done'
      AND r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) >= 6
      AND coalesce(r.enrichment_data::jsonb->>'mailing_lookup_deferred', '') <> 'true'
      AND (r.enrichment_data IS NULL
           OR NOT (r.enrichment_data::jsonb ? 'mailing_recovery_outcome'))
    ORDER BY lower(sc.county), r.job_id, r.id
"""

# Same eligibility re-asserted in the WHERE, so a row that changed between the read
# and the write (filled, deferred or concluded by something else) is a no-op.
_MARK_SQL = """
    UPDATE results SET enrichment_data =
        ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
               THEN enrichment_data::jsonb ELSE '{}'::jsonb END)
         || CAST(:payload AS jsonb))::json
    WHERE id = :rid
      AND user_id = :uid
      AND mailing_address IS NULL
      AND coalesce(enrichment_data::jsonb->>'mailing_lookup_deferred', '') <> 'true'
      AND (enrichment_data IS NULL
           OR NOT (enrichment_data::jsonb ? 'mailing_recovery_outcome'))
"""


def requeue(db, counties: list[str], *, apply: bool, report: Path | None = None) -> dict:
    """Mark eligible rows deferred. Returns counts; writes nothing unless ``apply``."""
    from src.scrapers.enrichment.county_gis import has_gis_mailing_source

    unsupported = [c for c in counties if not has_gis_mailing_source(c, "WA")]
    if unsupported:
        raise ValueError(f"no county GIS mailing source for: {', '.join(unsupported)}")

    rows = db.execute(text(_CANDIDATES_SQL), {"counties": counties}).all()
    stats: dict = {
        "candidates": len(rows),
        "by_county": dict(Counter(r.county for r in rows)),
        "distinct_parcels": len({(r.county, r.parcel_id.strip()) for r in rows}),
        "jobs": len({r.job_id for r in rows}),
        "duplicates": sum(1 for r in rows if r.is_duplicate),
        "marked": 0,
        "skipped_changed": 0,
    }
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "result_id": str(r.id), "job_id": str(r.job_id), "county": r.county,
                    "parcel_id": r.parcel_id, "is_duplicate": r.is_duplicate,
                    "has_property": r.has_property, "action": "mark" if apply else "dry-run",
                }) + "\n")
    if not apply:
        db.rollback()
        return stats

    payload = json.dumps({
        "mailing_lookup_deferred": True,
        "mailing_requeued_at": datetime.now(UTC).isoformat(),
        "mailing_requeue_reason": REASON,
    })
    for r in rows:
        result = db.execute(text(_MARK_SQL),
                            {"rid": r.id, "uid": r.user_id, "payload": payload})
        db.commit()
        if result.rowcount:
            stats["marked"] += 1
        else:
            stats["skipped_changed"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write markers (default: dry-run)")
    ap.add_argument("--counties", default=",".join(DEFAULT_COUNTIES),
                    help="comma-separated counties with a GIS mailing source")
    ap.add_argument("--report", type=Path,
                    default=Path(f"requeue_gis_mailing_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"),
                    help="JSON-lines evidence file (one line per candidate row)")
    args = ap.parse_args(argv)
    counties = [c.strip().lower() for c in args.counties.split(",") if c.strip()]

    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = requeue(db, counties, apply=args.apply, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
