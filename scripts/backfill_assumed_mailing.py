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

Every write: UPDATE guarded on BOTH addresses, owner flags recomputed with the
worker's compute_owner_flags, enrichment_data.mailing_source stamped, one JSONL
evidence line per candidate BEFORE any write, and rowcount checked (0 = the row
moved under us; reported as a conflict, never counted as written).

CONVERGENCE. Every row that is looked at leaves a durable
enrichment_data.mailing_backfill_status, and the candidate query excludes the
terminal ones. Without that the run cannot terminate: when the county says the
owner IS at the property, the new mailing still begins with the situs, so the row
matches the situs-copy signature again and the ordered LIMIT head never advances.
Six production runs of rule K wrote 180 rows while touching only 39 distinct ids
(23 of them re-written all six times) — the earlier "repeat until candidates: 0"
instruction could never have finished, and it re-hit a rate-limited source.

The owner flags take property_state from the CONFIG's state (the query is
sc.state='WA'-scoped), never from the frozen street-only property_address — that
parse always returns NULL, which is why every row this script had already touched
carried property_state IS NULL and out_of_state_owner IS NULL. --repair-flags
re-derives them for rows already stamped (needed for S and King-cleared rows,
whose mailing_address is NULL so they can never be re-selected as candidates).

    railway run --service worker python scripts/backfill_assumed_mailing.py                 # dry-run
    railway run --service worker python scripts/backfill_assumed_mailing.py --apply --rules S,P
    railway run --service worker python scripts/backfill_assumed_mailing.py --apply --rules K --king-limit 30
    railway run --service worker python scripts/backfill_assumed_mailing.py --repair-flags   # dry-run
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

_STATUS_KEY = "mailing_backfill_status"
# Terminal = this row has been decided against a real source and must never be
# reconsidered. Without this the LIMIT-30 head never advances: an owner-occupied
# King row is rewritten to a mailing that STILL starts with the situs, so it stays
# a candidate for ever (6 prod runs wrote 180 rows but touched only 39 ids).
_TERMINAL = ("resolved", "confirmed_same", "cleared_no_source", "not_found", "failed_terminal")
# A transient failure stays selectable, but it must not be selectable for ever: a row
# that errors every time would otherwise hold a slot in the ordered LIMIT head and
# starve untried rows. Retries are bounded, and retry rows sort AFTER untried ones.
_ATTEMPTS_KEY = "mailing_backfill_attempts"

# Prefix match without LIKE, so a '%' or '_' inside property_address cannot act as
# a wildcard (Codex P3). Proven equivalent against prod: LIKE and this form both
# select 20,277 rows, 0 lost / 0 gained. Codex's stricter "require a ',' delimiter"
# variant was REJECTED — it drops 9 real candidates whose stored situs is truncated
# ('20508 ISLAND PKWY' vs '20508 ISLAND PKWY E, LAKE TAPPS'), the exact shape the
# audit exists to fix.
_SITUS_COPY = (
    "left(upper(r.mailing_address), length(r.property_address)) = upper(r.property_address)"
)
_CANDIDATES = text(
    f"""
    SELECT r.id, r.parcel_id, r.property_address, r.mailing_address,
           r.property_city, r.property_zip, sc.county, sc.record_type, sc.state,
           COALESCE((r.enrichment_data ->> '{_ATTEMPTS_KEY}')::int, 0) AS attempts
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.state = 'WA' AND sc.county = :county AND sc.record_type = ANY(:types)
      AND r.parcel_id IS NOT NULL
      AND r.property_address IS NOT NULL AND btrim(r.property_address) <> ''
      AND r.mailing_address IS NOT NULL AND btrim(r.mailing_address) <> ''
      AND {_SITUS_COPY}
      AND COALESCE(r.enrichment_data ->> '{_STATUS_KEY}', '') <> ALL(:terminal)
    ORDER BY (COALESCE(r.enrichment_data ->> '{_STATUS_KEY}', '') = 'retry_later'),
             r.created_at, r.id
    """  # noqa: S608 — all interpolated fragments are module constants, not input
)

# jsonb_typeof guard: a historical row whose enrichment_data is a scalar or array
# would otherwise merge into the wrong top-level shape. Cast back to json — the
# column is Column(JSON), not JSONB (src/db/models.py:659).
_MERGE = """
        enrichment_data = (
            -- Drop the previous provenance/error first: jsonb `||` MERGES, it does not
            -- delete, so a row re-decided from resolved to not_found would otherwise keep
            -- a mailing_source it no longer has any claim to (Codex).
            ((CASE
                WHEN enrichment_data IS NULL THEN '{}'::jsonb
                WHEN jsonb_typeof(enrichment_data::jsonb) = 'object' THEN enrichment_data::jsonb
                ELSE '{}'::jsonb
              END) - 'mailing_source' - 'mailing_backfill_error')
            || jsonb_strip_nulls(jsonb_build_object(
                   'mailing_source', CAST(:source AS text),
                   'mailing_backfill_error', CAST(:error AS text)))
            || jsonb_build_object('mailing_backfill_status', CAST(:status AS text),
                                  'mailing_backfill_attempts', CAST(:attempts AS int))
        )::json
"""
# Guarded on BOTH addresses so a concurrent enrichment write loses the race
# loudly (rowcount 0) instead of being silently clobbered (Codex P2).
_WHERE = " WHERE id = :id AND mailing_address = :old AND property_address = :old_property "

_UPDATE = text(
    f"""
    UPDATE results
    SET mailing_address = :mailing,
        property_state = :property_state, owner_state = :owner_state,
        absentee_owner = :absentee_owner, out_of_state_owner = :out_of_state_owner,
    {_MERGE}
    {_WHERE}
    """  # noqa: S608 — module constants, not input
)
# Skips must still leave a durable mark, or a persistently failing head row pins
# the run for ever. Touches provenance/status only — never the address or flags.
_STAMP = text(
    f"""
    UPDATE results
    SET {_MERGE}
    {_WHERE}
    """  # noqa: S608 — module constants, not input
)
_RULES = {
    "S": ("snohomish", ["pre_foreclosure", "trustee_sale"]),
    "K": ("king", ["pre_foreclosure", "probate"]),
    "P": ("pierce", ["pre_foreclosure", "probate", "trustee_sale"]),
}

# --repair-flags: rows this backfill already stamped had their owner flags computed
# from the street-only situs, so property_state (and therefore out_of_state_owner)
# was written NULL on all of them. Rules S and K-cleared rows set mailing_address to
# NULL and so can never be re-selected as candidates — they need this pass. No
# external source is touched; the state comes from the config's own county.
_REPAIR = text(
    """
    SELECT r.id, r.property_address, r.mailing_address, r.property_city, r.property_zip,
           r.property_state, r.owner_state, r.absentee_owner, r.out_of_state_owner, sc.state
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.state = 'WA' AND sc.county = ANY(:counties)
      AND COALESCE(r.enrichment_data ->> 'mailing_source', '') <> ''
      AND r.property_address IS NOT NULL AND btrim(r.property_address) <> ''
    ORDER BY r.created_at, r.id
    """
)
_REPAIR_UPDATE = text(
    """
    UPDATE results
    SET property_state = :property_state, owner_state = :owner_state,
        absentee_owner = :absentee_owner, out_of_state_owner = :out_of_state_owner
    WHERE id = :id
      AND property_address = :old_property
      AND mailing_address IS NOT DISTINCT FROM :old
    """
)


class CountyLookupError(RuntimeError):
    pass


def decide(rule: str, old_mailing: str, lookup: dict | None) -> tuple[str, str | None, str, str]:
    """(action, new_mailing, provenance, status) — pure so the rules are unit-tested.

    action: "write" | "confirm" | "skip". lookup shapes:
      K: {"mailing_lookup": found|none|error|not_attempted, "mailing_address": str|None}
      P: {"mailing_address": county mailing} or None (parcel not in county layer)

    status is the DURABLE workflow state written to enrichment_data. Every row that
    is looked at must leave with one, so the ordered LIMIT-30 head always advances:
      resolved / confirmed_same / cleared_no_source / not_found  -> terminal
      retry_later                                                -> transient, retried
    It is deliberately separate from `provenance` (`mailing_source`), which stays a
    statement about where the ADDRESS came from and is never used for bookkeeping.
    """
    if rule == "S":
        return "write", None, "none_no_source", "cleared_no_source"
    if rule == "K":
        lk_status = (lookup or {}).get("mailing_lookup")
        if lk_status == "found" and lookup.get("mailing_address"):
            found = lookup["mailing_address"]
            # The assessor agreeing with the situs is REAL owner-occupied evidence,
            # so it is still written — but it must be recorded as decided, or the
            # row matches the situs-copy predicate again on the next run.
            same = _norm_mailing(found) == _norm_mailing(old_mailing)
            return "write", found, "king_assessor_tax_bill", "confirmed_same" if same else "resolved"
        if lk_status == "none":
            return "write", None, "none_king_assessor_no_mailing_block", "cleared_no_source"
        return "skip", None, f"king_lookup_{lk_status or 'missing'}", "retry_later"
    if rule == "P":
        if not lookup or not lookup.get("mailing_address"):
            # The county layer answered and does not carry this parcel — that is a
            # settled fact about the source, not a transient failure.
            return "skip", None, "pierce_parcel_not_in_county_layer", "not_found"
        county = lookup["mailing_address"]
        if _norm_mailing(county) == _norm_mailing(old_mailing):
            # Same place (a ZIP+4 suffix present on one side only is formatting
            # churn, not new information — keep the richer stored value, Codex P2).
            return "confirm", old_mailing, "pierce_county_gis", "confirmed_same"
        return "write", county, "pierce_county_gis", "resolved"
    raise ValueError(rule)


# `mailing_source` is only meaningful when we actually determined something about
# the mailing address; a not_found/retry_later row must not pollute provenance.
_PROVENANCE_STATUSES = frozenset({"resolved", "confirmed_same", "cleared_no_source"})


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


def _repair_flags(args) -> int:
    counties = [county for county, _ in _RULES.values()]
    totals = {"looked_at": 0, "changed": 0, "updated": 0, "stale": 0}
    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        rows = db.execute(_REPAIR, {"counties": counties}).fetchall()
        totals["looked_at"] = len(rows)
        print(f"repair-flags: {len(rows)} stamped row(s) in {counties}")
        for r in rows:
            flags = compute_owner_flags(
                r.property_address, r.mailing_address,
                property_city=r.property_city, property_state=r.state, property_zip=r.property_zip,
            )
            # Compare ALL FOUR flags: a row whose state was already right but whose
            # absentee_owner/owner_state is stale must not be skipped (Codex).
            stored = {"property_state": r.property_state, "owner_state": r.owner_state,
                      "absentee_owner": r.absentee_owner,
                      "out_of_state_owner": r.out_of_state_owner}
            if flags == stored:
                continue  # already correct — do not touch the row
            totals["changed"] += 1
            fh.write(json.dumps({
                "id": str(r.id), "property_address": r.property_address,
                "mailing_address": r.mailing_address, "was": stored, "now": flags,
            }, default=str) + "\n")
            if args.apply:
                rc = db.execute(_REPAIR_UPDATE, {
                    "id": r.id, "old_property": r.property_address, "old": r.mailing_address, **flags,
                }).rowcount or 0
                totals["updated"] += rc
                if rc != 1:
                    totals["stale"] += 1
                    print(f"  CONFLICT id={r.id} rowcount={rc} — row changed since read")
        db.commit() if args.apply else db.rollback()
    print(("applied: " if args.apply else "dry-run: ") + json.dumps(totals)
          + f" ; evidence -> {args.report}")
    return 0 if totals["stale"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rules", default="S,K,P", help="comma list of S,K,P")
    ap.add_argument("--king-limit", type=int, default=30, help="King rows per run (slow, rate-sensitive)")
    ap.add_argument("--king-pace", type=float, default=3.0, help="seconds between King page loads")
    ap.add_argument("--report", default=f"assumed_mailing_backfill_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="give up on a transiently-failing row after N attempts, so it "
                         "cannot hold a slot in the ordered head for ever")
    ap.add_argument("--repair-flags", action="store_true",
                    help="recompute owner flags for rows this backfill already stamped "
                         "(fixes property_state/out_of_state_owner written NULL); no source calls")
    args = ap.parse_args()
    if args.repair_flags:
        return _repair_flags(args)
    rules = [x.strip().upper() for x in args.rules.split(",") if x.strip()]
    unknown = [x for x in rules if x not in _RULES]
    if unknown or not rules:
        ap.error(f"--rules must be a comma list of {sorted(_RULES)}; got {args.rules!r}")

    totals = {"candidates": 0, "write": 0, "confirm": 0, "skip": 0, "updated": 0,
              "stamped": 0, "stale": 0}
    statuses: dict[str, int] = {}
    aborted = False
    with system_sync_session() as db, open(args.report, "w", encoding="utf-8") as fh:
        for rule in rules:
            county, types = _RULES[rule]
            rows = db.execute(
                _CANDIDATES, {"county": county, "types": types, "terminal": list(_TERMINAL)}
            ).fetchall()
            remaining = len(rows)
            if rule == "K":
                rows = rows[: args.king_limit]
                print(f"rule K: {remaining} undecided candidate(s); taking {len(rows)} this run")
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
                    aborted = True
                    continue
            elif rule == "K":
                try:
                    lookups = _king_rows(sorted({r.parcel_id.strip() for r in rows}), args.king_pace)
                except Exception as exc:  # noqa: BLE001 — source-health gate or transport
                    print(f"  ABORT rule K — King assessor unavailable: {str(exc)[:160]}")
                    continue
                # A globally blocked source must not burn a retry stamp on all 30 rows
                # (Codex P1). But the abort must be a HEALTH signal, not a result-shape
                # one, or a batch of genuinely-absent parcels stalls for ever (Codex).
                # So: 'none' and 'found' are real answers; 'not_attempted' is a deferral
                # that gets a bounded retry stamp; only an all-transport-failure batch
                # (every parcel errored or returned nothing at all) aborts the run.
                seen = [
                    ((lookups.get(r.parcel_id) or lookups.get(r.parcel_id.strip()) or {})
                     .get("mailing_lookup"))
                    for r in rows
                ]
                shape = {s or "missing": seen.count(s) for s in set(seen)}
                answered = any(s in ("found", "none") for s in seen)
                if seen and not answered and all(s in (None, "error") for s in seen):
                    print(f"  ABORT rule K — every parcel failed at the transport level "
                          f"({shape}); source-health gate tripped, nothing stamped. "
                          "This exits non-zero so it cannot stall silently.")
                    aborted = True
                    continue
            plans = []
            for r in rows:
                lk = lookups.get(r.parcel_id) or lookups.get(r.parcel_id.strip())
                action, new_mailing, source, status = decide(rule, r.mailing_address, lk)
                attempts = (r.attempts or 0) + 1
                if status == "retry_later" and attempts >= args.max_attempts:
                    # Give up loudly rather than hold a slot in the head for ever.
                    status = "failed_terminal"
                # The state is a FACT of the county this config scrapes (query is
                # sc.state='WA'-scoped), not an inference from the frozen street-only
                # situs line — parsing that line always yielded NULL, which is why
                # 1,286 prod rows carry property_state IS NULL / out_of_state IS NULL.
                flags = compute_owner_flags(
                    r.property_address,
                    new_mailing if action == "write" else r.mailing_address,
                    property_city=r.property_city,
                    property_state=r.state,
                    property_zip=r.property_zip,
                )
                plans.append((r, action, new_mailing, source, status, flags))
                totals[action] += 1
                statuses[status] = statuses.get(status, 0) + 1
                fh.write(json.dumps({
                    "rule": rule, "id": str(r.id), "parcel_id": r.parcel_id, "county": county,
                    "record_type": r.record_type, "property_address": r.property_address,
                    "old_mailing": r.mailing_address, "action": action, "new_mailing": new_mailing,
                    "provenance": source, "status": status, "lookup": lk, "flags": flags,
                }, default=str) + "\n")
                print(f"  {action:7} {status:15} {r.parcel_id:18} {r.mailing_address!r} -> "
                      f"{new_mailing!r} [{source}] absentee={flags['absentee_owner']} "
                      f"out_of_state={flags['out_of_state_owner']}")
            if args.apply:
                for r, action, new_mailing, source, status, flags in plans:
                    common = {
                        "id": r.id, "old": r.mailing_address, "old_property": r.property_address,
                        "status": status, "attempts": attempts,
                        "source": source if status in _PROVENANCE_STATUSES else None,
                        "error": source if status == "retry_later" else None,
                    }
                    if action in ("write", "confirm"):
                        rc = db.execute(_UPDATE, {
                            **common,
                            "mailing": new_mailing if action == "write" else r.mailing_address,
                            **flags,
                        }).rowcount or 0
                        totals["updated"] += rc
                    else:
                        # Still stamp, so this row cannot pin the ordered head for ever.
                        rc = db.execute(_STAMP, common).rowcount or 0
                        totals["stamped"] += rc
                    # rowcount 0 = the row moved under us; report it, never count it
                    # as written (Codex P2).
                    if rc != 1:
                        totals["stale"] += 1
                        print(f"  CONFLICT id={r.id} rowcount={rc} — row changed since read, not applied")
                db.commit()
        if not args.apply:
            db.rollback()
    print(("applied: " if args.apply else "dry-run: ") + json.dumps(totals)
          + " ; statuses: " + json.dumps(statuses) + f" ; evidence -> {args.report}")
    if aborted:
        # A silent stall is the failure mode to avoid: make the operator see it.
        print("EXIT 2 — a rule aborted on source health; nothing was stamped for it.")
        return 2
    return 0 if totals["stale"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
