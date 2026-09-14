"""Queue historical King tax leads for the background owner-name sweep.

Background (2026-09-14): King tax leads get their owner name from eRealProperty,
one page per parcel, and a job that loses the source lease or runs out of budget
leaves them unnamed. Job b2f2ecd5 delivered 840 King tax leads with 0 owner names.
Since #298 new jobs mark such leads `owner_lookup_deferred`, and since #303 the
beat sweep `src/workers/owner_recovery.recover_deferred_owners` names them. Leads
created before #298 carry no marker, so the sweep never sees them. This fixes those.

What it does, and all it does: put the `owner_lookup_deferred` marker on eligible
rows. It never looks anything up and never writes a name. The sweep then performs
the lookups under its own guards (kill switch, shared King source lease, 1 request
per second, breaker, parcel echo required, 120 parcels per 15-minute tick, fill-only,
tenant + parcel + job re-checked in the UPDATE, no billing, quota, job creation or
skip trace).

Eligible row: King WA tax_delinquent, job status done, not a duplicate, not over
quota (delivered), party_name blank, a 10-digit parcel, not already deferred, and no
owner outcome recorded by a job or by the sweep.

    railway run --service worker python scripts/requeue_king_tax_owner_recovery.py            # dry-run
    railway run --service worker python scripts/requeue_king_tax_owner_recovery.py --apply

Every candidate is written to --report (JSON lines) before any write.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

REASON = "historical_lead_before_owner_markers"

_ELIGIBLE = """
      r.is_duplicate = false
  AND r.enrichment_data::jsonb->>'delivery_excluded_reason' IS NULL
  AND (r.party_name IS NULL OR btrim(r.party_name) = '')
  AND btrim(r.parcel_id) ~ '^[0-9]{10}$'
  AND coalesce(r.enrichment_data::jsonb->>'owner_lookup_deferred', '') <> 'true'
  AND (r.enrichment_data IS NULL OR NOT (
        r.enrichment_data::jsonb ? 'owner_lookup_outcome'
        OR r.enrichment_data::jsonb ? 'owner_recovery_outcome'))
"""

_CANDIDATES_SQL = f"""
    SELECT r.id, r.user_id, r.job_id, btrim(r.parcel_id) AS parcel_id,
           r.property_address IS NOT NULL AS has_property, r.delinquent_amount
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
      AND j.status = 'done'
      AND {_ELIGIBLE}
    ORDER BY r.job_id, r.id
"""  # noqa: S608 -- splices only the _ELIGIBLE constant; every value is bound

# Same eligibility re-asserted in the WHERE, so a row that changed between the read
# and the write (named, capped, deferred or concluded by something else) is a no-op.
_MARK_SQL = f"""
    UPDATE results r SET enrichment_data =
        ((CASE WHEN jsonb_typeof(r.enrichment_data::jsonb) = 'object'
               THEN r.enrichment_data::jsonb ELSE '{{}}'::jsonb END)
         || CAST(:payload AS jsonb))::json
    WHERE r.id = :rid AND r.user_id = :uid
      AND {_ELIGIBLE}
"""  # noqa: S608 -- splices only the _ELIGIBLE constant; every value is bound


def requeue(db, *, apply: bool, report: Path | None = None) -> dict:
    """Mark eligible rows deferred. Returns counts; writes nothing unless ``apply``."""
    rows = db.execute(text(_CANDIDATES_SQL)).all()
    stats: dict = {
        "candidates": len(rows),
        "distinct_parcels": len({r.parcel_id for r in rows}),
        "jobs": len({r.job_id for r in rows}),
        "with_property_address": sum(1 for r in rows if r.has_property),
        "marked": 0,
        "skipped_changed": 0,
    }
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "result_id": str(r.id), "job_id": str(r.job_id), "parcel_id": r.parcel_id,
                    "delinquent_amount": str(r.delinquent_amount), "has_property": r.has_property,
                    "action": "mark" if apply else "dry-run",
                }) + "\n")
    if not apply:
        db.rollback()
        return stats

    payload = json.dumps({
        "owner_lookup_deferred": True,
        "owner_lookup_deferred_reason": REASON,
        "owner_requeued_at": datetime.now(UTC).isoformat(),
    })
    for r in rows:
        result = db.execute(text(_MARK_SQL), {"rid": r.id, "uid": r.user_id, "payload": payload})
        db.commit()
        stats["marked" if result.rowcount else "skipped_changed"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write markers (default: dry-run)")
    ap.add_argument("--report", type=Path,
                    default=Path(f"requeue_king_tax_owner_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"),
                    help="JSON-lines evidence file (one line per candidate row)")
    args = ap.parse_args(argv)

    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = requeue(db, apply=args.apply, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
