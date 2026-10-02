"""Backfill owner mailing addresses for ONE county's delivered leads, from its
county source. Dry run by default; `--apply` writes.

    railway run --service worker python scripts/backfill_mailing_by_county.py --county benton
    railway run --service worker python scripts/backfill_mailing_by_county.py --county benton --apply --report out.jsonl

WHAT IT TOUCHES
    results.mailing_address (fill-only: `WHERE mailing_address IS NULL`, the same
    guarded UPDATE the recovery sweep uses), the four owner-location flags derived
    from the new value, and bookkeeping keys in results.enrichment_data
    (mailing_source, mailing_recovery_outcome / _attempts / _last_at,
    mailing_lookup_deferred). Nothing else: no jobs row, no delivered_records, no
    quota, no billing, no skip-trace queue, no new rows.

WHAT IT SELECTS
    Rows of DONE jobs in the county with a parcel and no mailing address, whatever
    their deferral marker says (the marker was never written for these counties,
    which is why nothing ever revisited them). Rows the source has already settled
    (none / parcel_not_found / parcel_mismatch) and rows past the attempt cap are
    left alone, so a re-run after an interruption continues where it stopped.

PACE
    The county adapter paces and leases its own requests (one stream fleet-wide,
    403/429 cools the source for everyone). This script adds --batch (parcels per
    adapter call) and --max-parcels per run so a backfill is a bounded, repeatable
    unit, never a flood.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers.mailing_recovery import _MAX_ATTEMPTS, _write_row

SETTLED_NO_MAILING = frozenset({"none", "parcel_not_found", "parcel_mismatch"})

_CANDIDATES = """
SELECT r.id, r.user_id, r.parcel_id, r.property_address, r.property_city, r.property_state,
       r.property_zip,
       coalesce((r.enrichment_data::jsonb ->> 'mailing_recovery_attempts')::int, 0) AS attempts
FROM results r
JOIN jobs j ON j.id = r.job_id
JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE j.status = 'done'
  AND lower(sc.county) = :county AND upper(sc.state) = 'WA'
  AND r.mailing_address IS NULL
  AND r.parcel_id IS NOT NULL AND length(btrim(r.parcel_id)) >= 6
  AND coalesce(r.enrichment_data::jsonb ->> 'mailing_recovery_outcome', '')
      NOT IN ('none', 'parcel_not_found', 'parcel_mismatch')
  AND coalesce((r.enrichment_data::jsonb ->> 'mailing_recovery_attempts')::int, 0) < :max_attempts
  AND (coalesce(r.enrichment_data::jsonb ->> 'mailing_recovery_attempts', '0') ~ '^[0-9]+$')
ORDER BY r.created_at
LIMIT :limit
"""


def resolver_for(county: str):
    """(callable(parcel_ids) -> {pid: MailingAnswer}, source_tag) for a county, or None."""
    county = county.lower()
    from src.scrapers.enrichment import pacs_parcel

    if county in pacs_parcel.PACS_SITES:
        site = pacs_parcel.PACS_SITES[county]
        return (lambda ids: pacs_parcel.resolve_mailing(county, ids)), site.source_key
    if county == "thurston":
        from src.scrapers.enrichment import thurston_assessor

        return thurston_assessor.resolve_mailing, thurston_assessor.SOURCE
    if county == "snohomish":
        # The Assessor Roll shipped 2026-09-19; the 1,751 tax_delinquent rows from the
        # day before carry no deferral marker, so the sweep never revisited them.
        from src.scrapers.enrichment import snohomish_assessor_roll as roll

        return roll.resolve_mailing, "snohomish_assessor_roll"
    if county == "clark":
        try:
            from src.scrapers.enrichment import clark_pic
        except ImportError:
            return None
        return clark_pic.resolve_mailing, clark_pic.SOURCE
    return None


def run(county: str, *, apply: bool, batch: int, max_parcels: int, report=None) -> dict:
    found = resolver_for(county)
    if found is None:
        sys.exit(f"no county mailing source for {county!r}")
    resolve, source = found
    stats = {"county": county, "apply": apply, "rows": 0, "parcels": 0, "found": 0, "none": 0,
             "parcel_mismatch": 0, "unverified": 0, "errors": 0, "deferred": 0, "written": 0}
    with system_sync_session() as db:
        rows = db.execute(text(_CANDIDATES), {
            "county": county.lower(), "max_attempts": _MAX_ATTEMPTS, "limit": max_parcels * 4,
        }).mappings().all()
        by_parcel: dict[str, list] = {}
        for r in rows:
            by_parcel.setdefault(r["parcel_id"].strip(), []).append(r)
        parcels = list(by_parcel)[:max_parcels]
        stats["rows"] = sum(len(by_parcel[p]) for p in parcels)
        stats["parcels"] = len(parcels)
        print(f"{county}: {stats['rows']} candidate row(s) over {len(parcels)} parcel(s)"
              f" ({'APPLY' if apply else 'DRY RUN'})")
        for start in range(0, len(parcels), batch):
            chunk = parcels[start:start + batch]
            answers = resolve(chunk)
            now_iso = datetime.now(UTC).isoformat()
            for pid in chunk:
                answer = answers.get(pid)
                outcome = answer.outcome if answer else "source_unavailable"
                if outcome in ("source_unavailable",):
                    stats["deferred"] += 1
                    continue  # not reached: no attempt spent, nothing recorded
                terminal = outcome in SETTLED_NO_MAILING or bool(answer and answer.is_found)
                for row in by_parcel[pid]:
                    rec = {"row": str(row["id"])[:8], "parcel": pid, "outcome": outcome,
                           "mailing": bool(answer and answer.is_found)}
                    if report:
                        report.write(json.dumps(rec) + "\n")
                    if not apply:
                        continue
                    _write_row(
                        db, _Row(row), answer.mailing_address if answer and answer.is_found else None,
                        outcome, int(row["attempts"]) + 1, terminal, now_iso, stats,
                        source=source if answer and answer.is_found else None,
                    )
                    stats["written"] += 1
                key = {"found": "found", "none": "none", "parcel_mismatch": "parcel_mismatch",
                       "parcel_not_found": "none"}.get(outcome, "unverified")
                stats[key] += 1
    print(json.dumps(stats))
    return stats


class _Row:
    """Attribute view over a mapping row, for _write_row."""

    def __init__(self, m):
        self.id = m["id"]
        self.user_id = m["user_id"]
        self.parcel_id = m["parcel_id"]
        self.property_address = m["property_address"]
        self.property_city = m["property_city"]
        self.property_state = m["property_state"]
        self.property_zip = m["property_zip"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True)
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--batch", type=int, default=20, help="parcels per adapter call")
    ap.add_argument("--max-parcels", type=int, default=200, help="parcels per run")
    ap.add_argument("--report", help="JSONL of per-row outcomes (ids truncated, no addresses)")
    args = ap.parse_args()
    report = open(args.report, "a", encoding="utf-8") if args.report else None
    try:
        run(args.county, apply=args.apply, batch=args.batch, max_parcels=args.max_parcels, report=report)
    finally:
        if report:
            report.close()


if __name__ == "__main__":
    main()
