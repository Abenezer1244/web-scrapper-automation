"""Fill results.property_city / property_state / property_zip for EXISTING leads from
real sources, then recompute the four owner flags (audit item 4, Phase 5).

Sources, in evidence order (a part is filled only while still empty):
  1. notice   — the lead's linked Notice of Trustee Sale (results.nts_notice_id ->
                nts_notices.property_address, the "commonly known as" full situs)
  2. embedded — city/state/zip already inside results.property_address (a few King
                rows carry a trailing ZIP; the parse is anchored, nothing inferred)
  3. pierce   — county GIS City_State/Zipcode ONLY when Delivery_Address equals the
                Site_Address and is not a PO box (the county asserting mail goes to
                the property) — via the same _parse_gis_response rule the worker uses
  4. statewide — WA parcel layer SITUS_CITY_NM / SITUS_ZIP_NR by parcel + county FIPS
Rows with no source keep NULL parts and unknown flags ("unknown" is real data).

Every write: guarded UPDATE (parts still NULL), flags via compute_owner_flags with
the parts, one JSONL evidence line per row (source, raw values, parsed values).

    railway run --service worker python scripts/backfill_property_situs_parts.py            # dry-run
    railway run --service worker python scripts/backfill_property_situs_parts.py --apply
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
    _batch_query_county,
    _batch_query_wa_statewide,
    _KNOWN_GIS_ENDPOINTS,
)
from src.utils.address_intel import compute_owner_flags  # noqa: E402
from src.utils.lead_formatting import parse_property_for_display  # noqa: E402

_CANDIDATES = text(
    """
    SELECT r.id, r.parcel_id, r.property_address, r.mailing_address,
           r.property_city, r.property_state, r.property_zip,
           sc.county, sc.state, n.property_address AS notice_situs
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    LEFT JOIN nts_notices n ON n.id = r.nts_notice_id
    WHERE r.is_duplicate = false AND sc.state = 'WA'
      AND r.property_address IS NOT NULL
      AND (r.property_city IS NULL OR r.property_zip IS NULL)
    ORDER BY sc.county, r.created_at, r.id
    """
)
_UPDATE = text(
    """
    UPDATE results
    SET property_city = COALESCE(property_city, :city),
        property_state = COALESCE(property_state, :state),
        property_zip = COALESCE(property_zip, :zip),
        owner_state = :owner_state, absentee_owner = :absentee_owner,
        out_of_state_owner = :out_of_state_owner
    WHERE id = :id AND (property_city IS NULL OR property_zip IS NULL)
    """
)


def parts_from_line(line: str | None) -> dict[str, str | None]:
    """Structured parts parsed from a full address line (empty when absent)."""
    if not line:
        return {}
    p = parse_property_for_display(line)
    return {k: v for k, v in (("property_city", p.get("city")), ("property_state", p.get("state")),
                              ("property_zip", p.get("zip"))) if v}


def merge_parts(current: dict, *sources: tuple[str, dict]) -> tuple[dict, list[str]]:
    """Fill empty parts from sources in order; returns (parts, provenance per part)."""
    out = {k: current.get(k) for k in ("property_city", "property_state", "property_zip")}
    prov: list[str] = []
    for name, src in sources:
        for k in out:
            if not out[k] and src.get(k):
                out[k] = src[k]
                prov.append(f"{k}<-{name}")
    return out, prov


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=f"situs_parts_backfill_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl")
    args = ap.parse_args()

    totals = {"candidates": 0, "filled": 0, "no_source": 0, "updated": 0, "stale": 0,
              "absentee_false": 0, "absentee_true": 0, "out_of_state_true": 0}
    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        rows = db.execute(_CANDIDATES).fetchall()
        totals["candidates"] = len(rows)
        print(f"candidates: {len(rows)} lead(s) missing situs city/zip")

        # County + statewide lookups, batched per county, keyed by the caller's parcel id.
        by_county: dict[str, list[str]] = {}
        for r in rows:
            if r.parcel_id and len(r.parcel_id.strip()) >= 6:
                by_county.setdefault(r.county.lower(), []).append(r.parcel_id)
        county_rows: dict[tuple[str, str], dict] = {}
        for county, pids in by_county.items():
            pids = sorted(set(pids))
            cfg = _KNOWN_GIS_ENDPOINTS.get(f"{county}_WA")
            found = _batch_query_county(pids, cfg) if cfg else {}
            for pid, data in found.items():
                county_rows[(county, pid)] = data
            missing = [p for p in pids if p not in found]
            statewide = _batch_query_wa_statewide(missing, county) if missing else {}
            for pid, data in statewide.items():
                county_rows.setdefault((county, pid), data)
            print(f"  {county}: {len(pids)} parcels -> county {len(found)}, statewide {len(statewide)}")

        plans = []
        for r in rows:
            current = {"property_city": r.property_city, "property_state": r.property_state,
                       "property_zip": r.property_zip}
            gis = county_rows.get((r.county.lower(), r.parcel_id)) or {}
            gis_parts = {k: gis.get(k) for k in ("property_city", "property_state", "property_zip") if gis.get(k)}
            parts, prov = merge_parts(
                current,
                ("notice", parts_from_line(r.notice_situs)),
                ("embedded", parts_from_line(r.property_address)),
                ("gis", gis_parts),
            )
            gained = {k: v for k, v in parts.items() if v and not current.get(k)}
            flags = compute_owner_flags(
                r.property_address, r.mailing_address,
                property_city=parts["property_city"], property_state=parts["property_state"],
                property_zip=parts["property_zip"],
            )
            fh.write(json.dumps({
                "id": str(r.id), "county": r.county, "parcel_id": r.parcel_id,
                "property_address": r.property_address, "notice_situs": r.notice_situs,
                "gis_parts": gis_parts, "gained": gained, "provenance": prov, "flags": flags,
            }, default=str) + "\n")
            if not gained:
                totals["no_source"] += 1
                continue
            totals["filled"] += 1
            if flags["absentee_owner"] is False:
                totals["absentee_false"] += 1
            if flags["absentee_owner"] is True:
                totals["absentee_true"] += 1
            if flags["out_of_state_owner"] is True:
                totals["out_of_state_true"] += 1
            plans.append((r, parts, flags))

        if args.apply:
            for r, parts, flags in plans:
                rc = db.execute(_UPDATE, {
                    "id": r.id, "city": parts["property_city"], "state": parts["property_state"],
                    "zip": parts["property_zip"], "owner_state": flags["owner_state"],
                    "absentee_owner": flags["absentee_owner"],
                    "out_of_state_owner": flags["out_of_state_owner"],
                }).rowcount or 0
                totals["updated"] += rc
                totals["stale"] += 1 - rc
            db.commit()
        else:
            db.rollback()
    print(("applied: " if args.apply else "dry-run: ") + json.dumps(totals) + f" ; evidence -> {args.report}")
    return 0 if totals["stale"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
