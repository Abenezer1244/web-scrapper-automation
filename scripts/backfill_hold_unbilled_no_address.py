"""Hand past unbilled no-address leads to late delivery, so each is billed once (owner, 2026-11-16).

Before the no-address hold (commit 7831916c), a row its run counted "no address" and never billed
became a live lead once a sweep filled an address. The Phase 3 dry run (tasks/todo.md) found 45
such rows in seven runs of one account, every one still holding its property's claim. Owner
decision: bill them. This script only stamps `delivery_excluded_reason = no_address` on exactly
those rows; the deployed late-delivery tick (cv_late_delivery.py, NO_ADDRESS_LATE_DELIVERY_ENABLED)
then delivers and charges each one once, against the account's current quota window, and stamps
the late_delivery proof (policy "no_address").

A row is stamped only when ALL hold, decided under row locks in one transaction:
  * its run is one of --runs, done, billed, of --user, and not a code violation;
  * it is a live lead now (an address, not excluded, not a duplicate, not privacy-suppressed,
    never late-delivered) that had NEITHER address when its run was billed (property empty or
    recovered after billing, AND mailing empty or recovered after billing);
  * it still holds its claim (delivered_records.first_result_id = the row).
The count must equal --expect or nothing is written. Dry run by default. Ids and counts only.

Run AFTER the Phase 2 deploy, with the late-delivery switch on:
    railway run --service worker python scripts/backfill_hold_unbilled_no_address.py \
        --user <uuid> --runs <uuid,uuid,...> --expect 45            # dry run
    ... --apply                                                      # writes
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime

sys.path.insert(0, ".")  # railway-run: scripts/ is sys.path[0], repo root is not

from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, EXCLUSION_REASONS, NO_ADDRESS

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_hold_unbilled_no_address")

SOURCE_KEY = "no_address_hold_source"
SOURCE = "backfill_2026_11_16"
_EXCLUDED = ", ".join(f"'{r}'" for r in EXCLUSION_REASONS)
_PLACEHOLDER = "(enrichment unavailable)"


def _absent(col: str) -> str:
    return f"(nullif(btrim(r.{col}), '') IS NULL OR btrim(r.{col}) = '{_PLACEHOLDER}')"


_CANDIDATES_SQL = f"""
    SELECT r.id::text AS id, r.job_id::text AS job_id
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
    JOIN delivered_records dr ON dr.user_id = r.user_id AND dr.dedup_hash = r.dedup_hash
                             AND dr.first_result_id = r.id
    WHERE r.user_id = CAST(:uid AS uuid) AND r.job_id = ANY(CAST(:runs AS uuid[]))
      AND j.status = 'done' AND j.billing_applied_at IS NOT NULL
      AND sc.record_type <> 'code_violation'
      AND r.is_duplicate = false AND r.privacy_suppressed_at IS NULL
      AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
      AND coalesce(r.enrichment_data->>'{DELIVERY_EXCLUDED_KEY}', '') NOT IN ({_EXCLUDED})
      AND NOT (r.enrichment_data::jsonb ? 'late_delivery')
      AND NOT ({_absent('property_address')} AND {_absent('mailing_address')})
      AND ({_absent('property_address')}
           OR (r.enrichment_data->>'property_recovery_last_at')::timestamptz > j.billing_applied_at)
      AND ({_absent('mailing_address')}
           OR (r.enrichment_data->>'mailing_recovery_outcome' = 'found'
               AND (r.enrichment_data->>'mailing_recovery_last_at')::timestamptz > j.billing_applied_at))
    ORDER BY r.job_id, r.id
    FOR UPDATE OF r
"""  # noqa: S608 -- splices only module constants; every value is bound

_STAMP_SQL = """
    UPDATE results SET enrichment_data = (enrichment_data::jsonb || CAST(:stamp AS jsonb))::json
    WHERE user_id = CAST(:uid AS uuid) AND id = ANY(CAST(:ids AS uuid[]))
"""


def run(db, *, user_id: str, runs: list[str], expect: int, apply: bool) -> dict:
    """Select (and with apply, stamp) the rows; commits only a full, expected stamp."""
    rows = db.execute(text(_CANDIDATES_SQL), {"uid": user_id, "runs": runs}).mappings().all()
    per_run: dict[str, int] = {}
    for r in rows:
        per_run[r["job_id"][:8]] = per_run.get(r["job_id"][:8], 0) + 1
    out = {"found": len(rows), "per_run": per_run, "stamped": 0}
    if len(rows) != expect:
        db.rollback()
        out["refused"] = f"found {len(rows)}, expected {expect}: nothing written"
        return out
    if not apply:
        db.rollback()
        return out
    stamp = {DELIVERY_EXCLUDED_KEY: NO_ADDRESS,
             SOURCE_KEY: {"source": SOURCE, "at": datetime.now(UTC).isoformat()}}
    written = db.execute(text(_STAMP_SQL), {"uid": user_id, "ids": [r["id"] for r in rows],
                                            "stamp": json.dumps(stamp)}).rowcount
    if written != len(rows):
        db.rollback()
        out["refused"] = f"wrote {written} of {len(rows)}: rolled back"
        return out
    db.commit()
    out["stamped"] = written
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--runs", required=True, help="comma-separated run (job) ids")
    ap.add_argument("--expect", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from src.config import settings
    from src.db.session import system_sync_session

    if a.apply and not settings.NO_ADDRESS_LATE_DELIVERY_ENABLED:
        sys.exit("NO_ADDRESS_LATE_DELIVERY_ENABLED is off: stamped rows would only be hidden, never billed")
    with system_sync_session() as session:
        result = run(session, user_id=a.user, runs=[x.strip() for x in a.runs.split(",") if x.strip()],
                     expect=a.expect, apply=a.apply)
    _log.info("%s: %s", "APPLY" if a.apply else "DRY RUN", json.dumps(result))
    sys.exit(1 if "refused" in result else 0)
