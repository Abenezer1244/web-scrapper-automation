"""Give existing King code-violation leads a mailing address.

Every King code_violation lead was stored with parcel_id NULL (the Seattle SDCI feed has
no parcel number), so no mailing lookup could run for it. This locates each lead's
parcel from its stored coordinates under the strict rule in
`src/scrapers/enrichment/king_parcel_locate.py` and fills mailing from the Assessor
extract. Terminal jobs only; guarded single-row UPDATEs (still no mailing, still no
parcel_id, enrichment_data an object or null); owner flags recomputed; the PIN is
stored in enrichment_data.kc_pin, never parcel_id. No billing, quota, skip trace, job.

Every located row gets `kc_pin_status`, so a re-run only visits rows it never reached.

    railway run --service worker python scripts/backfill_king_code_violation_mailing.py        # dry-run
    railway run --service worker python scripts/backfill_king_code_violation_mailing.py --apply
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

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.property_address, r.property_city, r.property_state,
           r.property_zip, r.enrichment_data::jsonb->>'latitude' AS lat,
           r.enrichment_data::jsonb->>'longitude' AS lon
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'code_violation'
      AND j.status = 'done'
      AND r.parcel_id IS NULL
      AND r.mailing_address IS NULL
      AND r.enrichment_data::jsonb ? 'latitude'
      AND r.enrichment_data::jsonb ? 'longitude'
      AND NOT (r.enrichment_data::jsonb ? 'kc_pin_status')
    ORDER BY r.id
"""

_UPDATE_SQL = """
    UPDATE results SET
      mailing_address = CAST(:mail AS text),
      property_state = CASE WHEN CAST(:mail AS text) IS NOT NULL
                            THEN CAST(:f_property_state AS varchar) ELSE property_state END,
      owner_state = CASE WHEN CAST(:mail AS text) IS NOT NULL
                         THEN CAST(:f_owner_state AS varchar) ELSE owner_state END,
      absentee_owner = CASE WHEN CAST(:mail AS text) IS NOT NULL
                            THEN CAST(:f_absentee AS boolean) ELSE absentee_owner END,
      out_of_state_owner = CASE WHEN CAST(:mail AS text) IS NOT NULL
                                THEN CAST(:f_out_of_state AS boolean) ELSE out_of_state_owner END,
      enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
                               THEN enrichment_data::jsonb ELSE '{}'::jsonb END)
                         || CAST(:payload AS jsonb))::json
    WHERE id = :rid AND user_id = :uid
      AND mailing_address IS NULL AND parcel_id IS NULL
      AND (enrichment_data IS NULL OR jsonb_typeof(enrichment_data::jsonb) IN ('object', 'null'))
      AND NOT coalesce(enrichment_data::jsonb ? 'kc_pin_status', false)
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status = 'done')
"""


def run(db, *, apply_writes: bool, limit: int | None, report: Path | None,
        pace_s: float = 0.35) -> dict:
    from src.scrapers.enrichment.king_parcel_locate import SOURCE, resolve_code_violation_mailing
    from src.utils.address_intel import compute_owner_flags

    rows = db.execute(text(_CANDIDATES_SQL)).all()
    db.rollback()
    if limit:
        rows = rows[:limit]
    by_id = {str(r.id): r for r in rows}
    decisions, snapshot = resolve_code_violation_mailing(
        [(k, r.lat, r.lon, r.property_address) for k, r in by_id.items()], pace_s=pace_s)
    stats = {"candidates": len(rows), "snapshot": snapshot,
             "reached_with_status": len(decisions),
             "left_for_retry": len(rows) - len(decisions),
             "pin_status": dict(Counter(d["kc_pin_status"] for d in decisions.values())),
             "mailing_found": sum(1 for d in decisions.values() if d.get("mailing_address"))}
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for k, d in decisions.items():
                fh.write(json.dumps({"result_id": k, "property_address": by_id[k].property_address,
                                     **d}) + "\n")
    if not apply_writes:
        return stats
    written = skipped = 0
    now = datetime.now(UTC).isoformat()
    for i, (k, d) in enumerate(decisions.items(), 1):
        r = by_id[k]
        mail = d.get("mailing_address")
        flags = compute_owner_flags(r.property_address, mail, property_city=r.property_city,
                                    property_state=r.property_state, property_zip=r.property_zip)
        payload = {key: d[key] for key in ("kc_pin_status", "kc_pin", "kc_parcel_address",
                                           "kc_pin_match") if key in d}
        payload["kc_pin_checked_at"] = now
        if d.get("kc_pin"):
            payload["kc_pin_source"] = SOURCE
        if mail:
            payload.update({"mailing_source": "king_rpacct", "mailing_rpacct_snapshot": snapshot})
        res = db.execute(text(_UPDATE_SQL), {
            "mail": mail, "rid": r.id, "uid": r.user_id, "payload": json.dumps(payload),
            "f_property_state": flags["property_state"], "f_owner_state": flags["owner_state"],
            "f_absentee": flags["absentee_owner"], "f_out_of_state": flags["out_of_state_owner"],
        })
        written += bool(res.rowcount)
        skipped += not res.rowcount
        if i % 200 == 0:
            db.commit()
    db.commit()
    stats["writes"] = {"written": written, "skipped_by_write_guard": skipped}
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, help="only the first N candidates")
    ap.add_argument("--report", type=Path,
                    default=Path(f"king_cv_mailing_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = run(db, apply_writes=args.apply, limit=args.limit, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
