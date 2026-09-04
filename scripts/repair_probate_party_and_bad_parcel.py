"""Repair probate leads that carry a recorder placeholder / filing agency as the
party, and King leads enriched from a SILENTLY TRUNCATED parcel lookup.

Two independent repairs, both re-runnable and both dry-run by default.

PARTY  — King's LandmarkWeb Death Certificate index uses the literal placeholder
"PUBLIC" (the certificate is recorded "to the public") as the counterparty, and
indexes the SAME vital-records agency under three different word orders. Rows
written before the 2026-09-03 fix therefore carry a non-party in party_name
(25 rows) or in heirs (220 rows). The decedent is ALREADY STORED — in the heirs
column — so the repair is deterministic and needs no re-scrape: re-run the
corrected orient_probate_party over the stored (party_name, heirs, doc_type)
triple. Passing doc_type is required, not optional: without it the
Transfer-on-Death guard is bypassed and a LIVING owner would be swapped away
(Codex).

PARCEL — blue.kingcounty.com silently truncates an over-length ParcelNbr to the
first 10 digits and serves a DIFFERENT parcel's page with HTTP 200. Rows whose
parcel_id is not a well-formed 10-digit King PIN may therefore carry another
property's address and owner. Each candidate is RE-VERIFIED live against the
assessor before anything is cleared — a row is only touched when the county
itself echoes a different parcel than the one we asked for. Nothing is invented:
the wrong values become NULL, never a corrected guess. parcel_id itself is left
exactly as the county printed it (it feeds the FROZEN dedup_hash billing key,
and no 10-digit candidate can be derived without guessing).

Clearing a wrong property_address also cancels the skip trace it bought: a queued
pending_skip_trace_row for that lead is moved to 'errored' (the established
"will never be traced" terminal state the dispatcher skips and the UI renders as
"Error"), and the Result returns to 'not_attempted'. Two such rows were sitting
in 'queued' in production against a stranger's house.

    railway run --service worker python scripts/repair_probate_party_and_bad_parcel.py
    railway run --service worker python scripts/repair_probate_party_and_bad_parcel.py --apply
    railway run --service worker python scripts/repair_probate_party_and_bad_parcel.py --only party
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.enrichment.king_county_assessor import (  # noqa: E402
    _ERP_URL,
    _HEADERS,
    _extract_parcel_echo,
    parcel_page_is_for,
)
from src.scrapers.probate import orient_probate_party  # noqa: E402
from src.utils.safe_http import safe_get  # noqa: E402

_KING_PIN_DIGITS = 10

_PARTY_CANDIDATES = text(
    """
    SELECT r.id, r.party_name, r.heirs, r.doc_type, sc.county, sc.state
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.record_type IN ('probate', 'death_certificate')
      AND (r.party_name IS NOT NULL OR r.heirs IS NOT NULL)
    ORDER BY r.created_at
    """
)

# Guarded: the row must still hold the values we read.
_PARTY_UPDATE = text(
    """
    UPDATE results
    SET party_name = :new_party, heirs = :new_heirs
    WHERE id = :id
      AND party_name IS NOT DISTINCT FROM :old_party
      AND heirs IS NOT DISTINCT FROM :old_heirs
    """
)

_PARCEL_CANDIDATES = text(
    """
    SELECT r.id, r.parcel_id, r.property_address, r.property_city, r.property_state,
           r.property_zip, r.enrichment_data, r.skip_trace_status, sc.record_type
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) <> :pin_len
    ORDER BY r.created_at
    """
)

# The audited scope. Truncation is only PROVABLY harmful where the resolved parcel
# is contradicted by evidence we already hold: on the 5 probate rows the assessor's
# owner on the truncated parcel (SNYDER JACOB) contradicts the lead's own decedent
# (REINKE NORMAN LEONARD). On the 3 non-probate rows the truncation happened to land
# on the right parcel and the assessor owner CORROBORATES the lead's party, so
# clearing them would destroy correct data on a delivered lead. Those are reported,
# never silently cleared; widen with --record-types when a human decides to.
_DEFAULT_RECORD_TYPES = ("probate", "death_certificate")

_PARCEL_UPDATE = text(
    """
    UPDATE results
    SET property_address = NULL,
        property_city = NULL,
        property_state = NULL,
        property_zip = NULL,
        enrichment_data = CAST(:new_enrichment AS json)
    WHERE id = :id
      AND parcel_id = :parcel_id
      AND property_address IS NOT DISTINCT FROM :old_property
    """
)

_CANCEL_PENDING = text(
    """
    UPDATE pending_skip_trace_rows
    SET status = 'errored'
    WHERE result_id = :id
      AND status = 'queued'
    """
)

_RESET_RESULT_TRACE = text(
    """
    UPDATE results
    SET skip_trace_status = 'not_attempted'
    WHERE id = :id
      AND skip_trace_status = 'queued'
    """
)

# Enrichment keys derived FROM the assessor page we now know was the wrong parcel.
_ASSESSOR_DERIVED_KEYS = ("assessor_current_owner", "title_status")


def _journal(path: str, payload: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


def repair_party(db, *, apply: bool, journal: str) -> dict:
    rows = db.execute(_PARTY_CANDIDATES).mappings().all()
    stats = {"scanned": len(rows), "changed": 0, "party_fixed": 0, "heirs_fixed": 0,
             "no_party_left": 0, "written": 0}
    for row in rows:
        new_party, new_heirs = orient_probate_party(row["party_name"], row["heirs"], row["doc_type"])
        if new_party == row["party_name"] and new_heirs == row["heirs"]:
            continue
        stats["changed"] += 1
        if new_party != row["party_name"]:
            stats["party_fixed"] += 1
        if new_heirs != row["heirs"]:
            stats["heirs_fixed"] += 1
        if new_party is None:
            # Both sides were non-parties. Leave the row exactly as it is: this
            # script repairs identity, it does not delete delivered leads. The
            # scraper fix stops new ones being created; these are reported so the
            # decision to remove them stays a human one.
            stats["no_party_left"] += 1
            _journal(journal, {"repair": "party", "action": "skipped_no_party",
                               "id": row["id"], "party": row["party_name"], "heirs": row["heirs"]})
            continue
        _journal(journal, {"repair": "party", "action": "apply" if apply else "dry_run",
                           "id": row["id"], "county": row["county"],
                           "old_party": row["party_name"], "new_party": new_party,
                           "old_heirs": row["heirs"], "new_heirs": new_heirs})
        if apply:
            res = db.execute(_PARTY_UPDATE, {
                "id": row["id"], "new_party": new_party, "new_heirs": new_heirs,
                "old_party": row["party_name"], "old_heirs": row["heirs"],
            })
            stats["written"] += res.rowcount
    if apply:
        db.commit()
    return stats


def repair_bad_parcel(db, *, apply: bool, journal: str,
                      record_types: tuple[str, ...] = _DEFAULT_RECORD_TYPES) -> dict:
    rows = db.execute(_PARCEL_CANDIDATES, {"pin_len": _KING_PIN_DIGITS}).mappings().all()
    stats = {"scanned": len(rows), "verified_ok": 0, "mismatched": 0, "cleared": 0,
             "traces_cancelled": 0, "lookup_failed": 0, "out_of_scope": 0}
    # One live lookup per DISTINCT parcel, not per row.
    verdicts: dict[str, tuple[bool, str | None]] = {}
    for row in rows:
        pid = row["parcel_id"].strip()
        if pid not in verdicts:
            try:
                resp = safe_get(f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=15)
                if resp.status_code != 200:
                    verdicts[pid] = (True, None)  # unknown -> treat as OK, change nothing
                    stats["lookup_failed"] += 1
                else:
                    verdicts[pid] = (parcel_page_is_for(resp.text, pid),
                                     _extract_parcel_echo(resp.text))
            except Exception as exc:  # noqa: BLE001 — a lookup failure must never clear a row
                print(f"  lookup failed for {pid}: {type(exc).__name__}: {str(exc)[:120]}")
                verdicts[pid] = (True, None)
                stats["lookup_failed"] += 1
        ok, echoed = verdicts[pid]
        if ok:
            stats["verified_ok"] += 1
            continue
        stats["mismatched"] += 1
        if row["record_type"] not in record_types:
            # Reported, never silently cleared — see _DEFAULT_RECORD_TYPES.
            stats["out_of_scope"] += 1
            print(f"  OUT OF SCOPE ({row['record_type']}) result={row['id']} "
                  f"parcel={pid} county_echoed={echoed} "
                  f"property_address={row['property_address']!r}")
            _journal(journal, {"repair": "parcel", "action": "reported_out_of_scope",
                               "id": row["id"], "parcel_id": pid, "county_echoed": echoed,
                               "record_type": row["record_type"],
                               "property_address": row["property_address"]})
            continue
        enrichment = dict(row["enrichment_data"] or {})
        removed = {k: enrichment.pop(k) for k in _ASSESSOR_DERIVED_KEYS if k in enrichment}
        enrichment["parcel_lookup"] = "mismatch"
        enrichment["parcel_echoed_by_county"] = echoed
        _journal(journal, {"repair": "parcel", "action": "apply" if apply else "dry_run",
                           "id": row["id"], "parcel_id": pid, "county_echoed": echoed,
                           "cleared_property_address": row["property_address"],
                           "cleared_enrichment": removed,
                           "skip_trace_status": row["skip_trace_status"]})
        if apply:
            res = db.execute(_PARCEL_UPDATE, {
                "id": row["id"], "parcel_id": row["parcel_id"],
                "old_property": row["property_address"],
                "new_enrichment": json.dumps(enrichment),
            })
            stats["cleared"] += res.rowcount
            cancelled = db.execute(_CANCEL_PENDING, {"id": row["id"]}).rowcount
            if cancelled:
                db.execute(_RESET_RESULT_TRACE, {"id": row["id"]})
            stats["traces_cancelled"] += cancelled
    if apply:
        db.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--only", choices=("party", "parcel"), help="run just one repair")
    ap.add_argument("--journal", default=None, help="JSONL evidence file")
    ap.add_argument(
        "--record-types", default=",".join(_DEFAULT_RECORD_TYPES),
        help="record types the PARCEL repair may clear (others are reported only)",
    )
    args = ap.parse_args()
    record_types = tuple(t.strip() for t in args.record_types.split(",") if t.strip())

    journal = args.journal or (
        f"repair_probate_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        f"{'' if args.apply else '_dryrun'}.jsonl"
    )
    print(f"mode={'APPLY' if args.apply else 'DRY RUN'}  journal={journal}")

    with system_sync_session() as db:
        if args.only != "parcel":
            print("\n-- party / heirs re-orientation --")
            print(json.dumps(repair_party(db, apply=args.apply, journal=journal), indent=1))
        if args.only != "party":
            print(f"\n-- King malformed-parcel enrichment (clearing scope: "
                  f"{', '.join(record_types)}) --")
            print(json.dumps(
                repair_bad_parcel(db, apply=args.apply, journal=journal,
                                  record_types=record_types),
                indent=1,
            ))


if __name__ == "__main__":
    main()
