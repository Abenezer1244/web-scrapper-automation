"""Repair Pierce leads whose mailing_address is the fabricated statewide "situs, WA" line.

Background (2026-09-02 audit, PR #184): the county-GIS batch enrichment keyed its
results by the server's canonical parcel while the worker applies rows by the lead's
raw parcel_id, so dashed parcels (and any row whose county call failed at scrape time)
fell through to the WA statewide SITUS-only service, whose mailing builder appended
", WA" even with null city/zip. Result: mailing_address == property_address + ", WA" —
a fabricated line that was demonstrably WRONG for some owners (the county has a PO BOX
or a different street on file). PR #184 stops new ones; this repairs the existing rows.

Per row (Pierce WA only, exact fabricated signature only):
  * Pierce Tax_Parcels GIS has the parcel  -> mailing_address = the county's real
    Delivery_Address + City_State + Zipcode (the same builder the worker uses).
  * county response succeeded but lacks it  -> mailing_address = NULL (the statewide
    service has no mailing data; unknown beats a fabricated owner-occupied line).
  * county request FAILED (non-200 / exception / bad schema) -> ABORT, no writes.
Then the four owner flags are recomputed with the worker's compute_owner_flags and
written in the SAME guarded UPDATE (WHERE id=:id AND mailing_address = :old), so a
concurrent change makes the write a visible no-op. property_address (dedup-bearing,
skip-trace key) is never touched; no skip-trace re-dispatch.

Every row's decision is written to --report (JSON lines) before any write so a later
reader can tell "county genuinely absent" from "backfill bug" (Codex).

    railway run --service worker python scripts/backfill_pierce_statewide_mailing.py            # dry-run
    railway run --service worker python scripts/backfill_pierce_statewide_mailing.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.enrichment.county_gis import (  # noqa: E402
    _KNOWN_GIS_ENDPOINTS,
    _arcgis_literal,
    _map_county_features,
)
from src.utils.address_intel import compute_owner_flags  # noqa: E402
from src.utils.safe_http import safe_get  # noqa: E402

_COUNTY_KEY = "pierce_WA"
_CHUNK = 50

# Exact fabricated signature: the statewide builder produced situs + ", WA" and nothing
# else in the pipeline does. A legitimate zip-less WA mailing line (e.g. "PO BOX 1,
# TACOMA, WA") is NOT selected (Codex P1).
_CANDIDATES = text(
    """
    SELECT r.id, r.parcel_id, r.property_address, r.mailing_address
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.county = 'pierce' AND sc.state = 'WA'
      AND sc.record_type IN ('pre_foreclosure', 'trustee_sale')
      AND r.parcel_id IS NOT NULL
      AND r.mailing_address = r.property_address || ', WA'
    ORDER BY r.created_at
    """
)
_UPDATE = text(
    """
    UPDATE results
    SET mailing_address = :mailing,
        property_state = :property_state,
        owner_state = :owner_state,
        absentee_owner = :absentee_owner,
        out_of_state_owner = :out_of_state_owner
    WHERE id = :id AND mailing_address = :old
    """
)


class CountyLookupError(RuntimeError):
    """The county GIS request itself failed — nothing may be inferred from it."""


def county_rows(parcel_ids: list[str]) -> dict[str, dict]:
    """Pierce county GIS rows keyed by the CALLER's raw parcel id. Raises on any
    request-level failure so a transport blip can never be read as "not found"."""
    cfg = _KNOWN_GIS_ENDPOINTS[_COUNTY_KEY]
    found: dict[str, dict] = {}
    for i in range(0, len(parcel_ids), _CHUNK):
        chunk = parcel_ids[i:i + _CHUNK]
        clean_to_originals: dict[str, list[str]] = {}
        for pid in chunk:
            # dashes only; leading zeros kept; never numeric (Codex)
            clean_to_originals.setdefault(pid.replace("-", "").strip(), []).append(pid)
        params = {
            "where": f"{cfg['parcel_field']} IN ({','.join(_arcgis_literal(p) for p in clean_to_originals)})",
            "outFields": cfg["out_fields"],
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": _CHUNK,
        }
        try:
            resp = safe_get(cfg["endpoint"], params=params, require_allowlisted=False, timeout=30)
        except Exception as exc:  # noqa: BLE001 — surfaced as a hard abort below
            raise CountyLookupError(f"county GIS request error: {str(exc)[:160]}") from exc
        if resp.status_code != 200:
            raise CountyLookupError(f"county GIS HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data or "features" not in data:
            raise CountyLookupError(f"county GIS bad response: {str(data)[:160]}")
        found.update(_map_county_features(data["features"], cfg, clean_to_originals))
    return found


def plan_row(property_address: str | None, county: dict | None) -> tuple[str | None, str]:
    """(new_mailing, action) for one row given the county lookup outcome."""
    if county and county.get("mailing_address"):
        return county["mailing_address"], "county_mailing"
    return None, "null_no_county_row"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument(
        "--report", default=f"pierce_mailing_backfill_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl",
        help="JSON-lines evidence file (one line per candidate row)",
    )
    args = ap.parse_args()

    with system_sync_session() as db:
        rows = db.execute(_CANDIDATES).fetchall()
        print(f"candidates: {len(rows)} Pierce row(s) with the fabricated 'situs, WA' mailing")
        if not rows:
            return 0
        try:
            county = county_rows(sorted({r.parcel_id for r in rows}))
        except CountyLookupError as exc:
            print(f"ABORT — {exc}; no rows written")
            return 2

        plans = []
        for r in rows:
            crow = county.get(r.parcel_id)
            new_mailing, action = plan_row(r.property_address, crow)
            flags = compute_owner_flags(r.property_address, new_mailing)
            plans.append((r, crow, new_mailing, action, flags))

        with open(args.report, "w", encoding="utf-8") as fh:
            for r, crow, new_mailing, action, flags in plans:
                fh.write(json.dumps({
                    "id": str(r.id), "parcel_id": r.parcel_id,
                    "property_address": r.property_address, "old_mailing": r.mailing_address,
                    "county_found": crow is not None, "action": action,
                    "new_mailing": new_mailing, "flags": flags,
                }) + "\n")
        counts = {"county_found": sum(1 for p in plans if p[1]),
                  "county_not_found": sum(1 for p in plans if not p[1])}
        print(f"county lookup: {counts} ; evidence -> {args.report}")
        for r, _crow, new_mailing, action, flags in plans:
            print(f"  {r.parcel_id:14} {action:18} {r.mailing_address!r} -> {new_mailing!r} "
                  f"absentee={flags['absentee_owner']}")

        if not args.apply:
            print("dry-run: nothing written (re-run with --apply)")
            db.rollback()
            return 0

        updated = skipped = 0
        for r, _crow, new_mailing, _action, flags in plans:
            rc = db.execute(_UPDATE, {
                "id": r.id, "old": r.mailing_address, "mailing": new_mailing, **flags,
            }).rowcount or 0
            updated += rc
            skipped += 1 - rc
        db.commit()
        print(f"applied: candidates={len(plans)} updated={updated} skipped_concurrent={skipped}")
        if updated != len(plans):
            print("WARNING: not every candidate was updated — inspect the evidence file")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
