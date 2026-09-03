"""Replace ASSUMED mailing addresses with real data or NULL (audit item 3, Phase 2).

Before 2026-09-02 the statewide situs-only lookup copied the property address into
mailing_address (an owner-occupied assumption written as data), and because the
King assessor pass only runs for rows with NO mailing, that copy also pre-empted the
real King lookup. Product decision: "it should not assume the owner lives at the
property — real data everywhere." Rules, per county (all scoped to non-tax leads
whose mailing still BEGINS WITH the property street = the situs-copy signature):

  S  snohomish pre_foreclosure/trustee_sale: no real mailing source exists in the
     pipeline -> mailing NULL (unknown), provenance "none_no_source".
  K  king pre_foreclosure/probate: recover from the King assessor (tax-bill page),
     --king-limit rows per run, slow pace, abort when the source-health gate trips.
     lookup "found" -> the assessor value (even when it equals the situs: that is
     REAL owner-occupied evidence); "none" (page proven, no mailing block) -> NULL;
     "error"/"not_attempted" -> unchanged.
  P  pierce (all non-tax types): county GIS Delivery_Address is a real source —
     verify against the live county row; refresh when it differs, confirm when
     equal, leave untouched when the county layer lacks the parcel.

Every write: guarded UPDATE (mailing_address = :old), owner flags recomputed with
the worker's compute_owner_flags, enrichment_data.mailing_source stamped, one
JSONL evidence line per candidate BEFORE any write.

    railway run --service worker python scripts/backfill_assumed_mailing.py                 # dry-run
    railway run --service worker python scripts/backfill_assumed_mailing.py --apply --rules S,P
    railway run --service worker python scripts/backfill_assumed_mailing.py --apply --rules K --king-limit 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
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

_SITUS_COPY = "upper(r.mailing_address) LIKE upper(r.property_address) || '%'"
_CANDIDATES = text(
    f"""
    SELECT r.id, r.parcel_id, r.property_address, r.mailing_address, sc.county, sc.record_type
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.state = 'WA' AND sc.county = :county AND sc.record_type = ANY(:types)
      AND r.parcel_id IS NOT NULL AND r.property_address IS NOT NULL
      AND r.mailing_address IS NOT NULL AND {_SITUS_COPY}
    ORDER BY r.created_at, r.id
    """  # noqa: S608 — the interpolated fragment is a module constant, not input
)
_UPDATE = text(
    """
    UPDATE results
    SET mailing_address = :mailing,
        property_state = :property_state, owner_state = :owner_state,
        absentee_owner = :absentee_owner, out_of_state_owner = :out_of_state_owner,
        enrichment_data = COALESCE(enrichment_data, '{}'::json)::jsonb
                          || jsonb_build_object('mailing_source', CAST(:source AS text))
    WHERE id = :id AND mailing_address = :old
    """
)
_RULES = {
    "S": ("snohomish", ["pre_foreclosure", "trustee_sale"]),
    "K": ("king", ["pre_foreclosure", "probate"]),
    "P": ("pierce", ["pre_foreclosure", "probate", "trustee_sale"]),
}


class CountyLookupError(RuntimeError):
    pass


def decide(rule: str, old_mailing: str, lookup: dict | None) -> tuple[str, str | None, str]:
    """(action, new_mailing, provenance) — pure so the rules are unit-tested.

    action: "write" | "confirm" | "skip". lookup shapes:
      K: {"mailing_lookup": found|none|error|not_attempted, "mailing_address": str|None}
      P: {"mailing_address": county mailing} or None (parcel not in county layer)
    """
    if rule == "S":
        return "write", None, "none_no_source"
    if rule == "K":
        status = (lookup or {}).get("mailing_lookup")
        if status == "found" and lookup.get("mailing_address"):
            return "write", lookup["mailing_address"], "king_assessor_tax_bill"
        if status == "none":
            return "write", None, "none_king_assessor_no_mailing_block"
        return "skip", None, f"king_lookup_{status or 'missing'}"
    if rule == "P":
        if not lookup or not lookup.get("mailing_address"):
            return "skip", None, "pierce_parcel_not_in_county_layer"
        county = lookup["mailing_address"]
        if _norm_mailing(county) == _norm_mailing(old_mailing):
            # Same place (a ZIP+4 suffix present on one side only is formatting
            # churn, not new information — keep the richer stored value, Codex P2).
            return "confirm", old_mailing, "pierce_county_gis"
        return "write", county, "pierce_county_gis"
    raise ValueError(rule)


def _norm_mailing(value: str) -> str:
    s = " ".join(value.upper().replace(",", " ").split())
    return re.sub(r"\b(\d{5})-\d{4}\b", r"\1", s)


def _pierce_rows(parcel_ids: list[str]) -> dict[str, dict]:
    cfg = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
    found: dict[str, dict] = {}
    for i in range(0, len(parcel_ids), 50):
        chunk = parcel_ids[i:i + 50]
        clean_to_originals: dict[str, list[str]] = {}
        for pid in chunk:
            clean_to_originals.setdefault(pid.replace("-", "").strip(), []).append(pid)
        params = {
            "where": f"{cfg['parcel_field']} IN ({','.join(_arcgis_literal(p) for p in clean_to_originals)})",
            "outFields": cfg["out_fields"], "returnGeometry": "false", "f": "json",
            "resultRecordCount": 50,
        }
        try:
            resp = safe_get(cfg["endpoint"], params=params, require_allowlisted=False, timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise CountyLookupError(str(exc)[:160]) from exc
        if resp.status_code != 200:
            raise CountyLookupError(f"HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data or "features" not in data:
            raise CountyLookupError(str(data)[:160])
        found.update(_map_county_features(data["features"], cfg, clean_to_originals))
    return found


def _king_rows(parcel_ids: list[str], pace_s: float) -> dict[str, dict]:
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
    return asyncio.run(batch_enrich_king_county(parcel_ids, pace_s=pace_s))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rules", default="S,K,P", help="comma list of S,K,P")
    ap.add_argument("--king-limit", type=int, default=30, help="King rows per run (slow, rate-sensitive)")
    ap.add_argument("--king-pace", type=float, default=3.0, help="seconds between King page loads")
    ap.add_argument("--report", default=f"assumed_mailing_backfill_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl")
    args = ap.parse_args()
    rules = [x.strip().upper() for x in args.rules.split(",") if x.strip()]
    unknown = [x for x in rules if x not in _RULES]
    if unknown or not rules:
        ap.error(f"--rules must be a comma list of {sorted(_RULES)}; got {args.rules!r}")

    totals = {"candidates": 0, "write": 0, "confirm": 0, "skip": 0, "updated": 0, "stale": 0}
    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        for rule in rules:
            county, types = _RULES[rule]
            rows = db.execute(_CANDIDATES, {"county": county, "types": types}).fetchall()
            if rule == "K":
                rows = rows[: args.king_limit]
            print(f"rule {rule} ({county}): {len(rows)} candidate row(s)")
            totals["candidates"] += len(rows)
            if not rows:
                continue
            lookups: dict[str, dict] = {}
            if rule == "P":
                try:
                    lookups = _pierce_rows(sorted({r.parcel_id for r in rows}))
                except CountyLookupError as exc:
                    print(f"  ABORT rule P — county GIS request failed: {exc}")
                    continue
            elif rule == "K":
                try:
                    lookups = _king_rows(sorted({r.parcel_id.strip() for r in rows}), args.king_pace)
                except Exception as exc:  # noqa: BLE001 — source-health gate or transport
                    print(f"  ABORT rule K — King assessor unavailable: {str(exc)[:160]}")
                    continue
            plans = []
            for r in rows:
                lk = lookups.get(r.parcel_id) or lookups.get(r.parcel_id.strip())
                action, new_mailing, source = decide(rule, r.mailing_address, lk)
                flags = compute_owner_flags(r.property_address, new_mailing if action == "write" else r.mailing_address)
                plans.append((r, action, new_mailing, source, flags))
                totals[action] += 1
                fh.write(json.dumps({
                    "rule": rule, "id": str(r.id), "parcel_id": r.parcel_id, "county": county,
                    "record_type": r.record_type, "property_address": r.property_address,
                    "old_mailing": r.mailing_address, "action": action, "new_mailing": new_mailing,
                    "provenance": source, "lookup": lk, "flags": flags,
                }, default=str) + "\n")
                print(f"  {action:7} {r.parcel_id:18} {r.mailing_address!r} -> {new_mailing!r} [{source}] absentee={flags['absentee_owner']}")
            if args.apply:
                for r, action, new_mailing, source, flags in plans:
                    if action not in ("write", "confirm"):
                        continue
                    rc = db.execute(_UPDATE, {
                        "id": r.id, "old": r.mailing_address, "mailing": new_mailing if action == "write" else r.mailing_address,
                        "source": source, **flags,
                    }).rowcount or 0
                    totals["updated"] += rc
                    totals["stale"] += 1 - rc
                db.commit()
        if not args.apply:
            db.rollback()
    print(("applied: " if args.apply else "dry-run: ") + json.dumps(totals) + f" ; evidence -> {args.report}")
    return 0 if totals["stale"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
