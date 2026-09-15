"""Fill missing King property addresses on delivered leads: condo units + account numbers.

WHY: King's GIS layer has no condo UNIT features, so a unit's property address came
only from the per-parcel eRealProperty page, and every King job that ran while that
source was busy or throttled shipped the unit with no property address (pre-foreclosure
job 85692303 on 2026-09-13: 45 of 47 blanks). Recorder rows that printed the 12-digit
tax ACCOUNT number instead of the PIN never matched anything. New jobs handle both since
the Phase 1 enrichment change; this repairs leads delivered before it.

Decisions use the SAME rules as the live enrichment pass (king_condo_units.unit_situs /
compose_fill, king_rpacct.load_account_pins):

  * account number -> PIN only on an exact, unanimous extract match, recorder rows only;
    the PIN is recorded beside parcel_id (enrichment_data.resolved_*), never in it.
  * property address from the resolved PIN's King GIS situs, or from the condo unit
    extract as "STREET, CITY, ST ZIP" when the complex's GIS ZIP equals the unit's ZIP.
  * anything else is left EMPTY and counted. Mailing is never copied into property.

Every write is a guarded single-row UPDATE: same id, user_id and parcel_id, property
address still empty, job still done, enrichment_data still mergeable. It stamps the
source, the extract date and the repair reason, and recomputes the owner-location flags
from the new address. Nothing touches parcel_id, dedup_hash, billing, quota, delivery,
skip-trace state or contact data; no job is created. Re-running it writes nothing new.

    railway run --service worker python scripts/repair_king_property_situs.py            # dry-run
    railway run --service worker python scripts/repair_king_property_situs.py --apply
    ... --record-type tax_delinquent     (default pre_foreclosure)
    ... --job-id <uuid>                  (one job only)
    ... --rpacct-zip PATH --condo-zip PATH --snapshot YYYY-MM-DD   (skip the downloads)

A JSON-lines evidence file records every candidate and decision before any write.
Requests: one GIS query per 50 parcels (public ArcGIS layer). No eRealProperty calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from src.workers.tasks_helpers.enrich import KING_ACCOUNT_RESOLVER  # noqa: E402

REASON_CONDO = "king_condo_unit"
REASON_ACCOUNT = "king_account_number"
REASON_GIS = "king_gis_retry"
_PIN_RE = re.compile(r"\d{10}")
_ACCOUNT_RE = re.compile(r"\d{12}")

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.job_id, r.parcel_id AS raw_pid, btrim(r.parcel_id) AS pid,
           r.mailing_address, r.property_city, r.property_state, r.property_zip,
           r.enrichment_data::jsonb->>'source' AS row_source,
           r.enrichment_data::jsonb->>'resolved_by' AS resolved_by,
           r.enrichment_data::jsonb->>'resolved_parcel_id' AS resolved_pin
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = :record_type
      AND j.status = 'done'
      AND (CAST(:job_id AS text) IS NULL OR j.id = CAST(:job_id AS uuid))
      AND r.is_duplicate = false
      AND r.enrichment_data::jsonb->>'delivery_excluded_reason' IS NULL
      AND coalesce(btrim(r.property_address), '') IN ('', '(enrichment unavailable)')
      AND btrim(coalesce(r.parcel_id, '')) ~ '^[0-9]{10}$|^[0-9]{12}$'
    ORDER BY r.id
"""

# Fully static: every value is a bound parameter. enrichment_data merges only into a JSON
# object (JSON null counts as empty); an array or scalar is skipped, never replaced.
_UPDATE_SQL = """
    UPDATE results SET
      property_address = :address,
      property_city = COALESCE(property_city, CAST(:city AS varchar)),
      property_state = COALESCE(property_state, CAST(:state AS varchar)),
      property_zip = COALESCE(property_zip, CAST(:zip AS varchar)),
      owner_state = CAST(:f_owner_state AS varchar),
      absentee_owner = CAST(:f_absentee AS boolean),
      out_of_state_owner = CAST(:f_out_of_state AS boolean),
      enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
                               THEN enrichment_data::jsonb ELSE '{}'::jsonb END)
                         || CAST(:patch AS jsonb))::json
    WHERE id = :rid AND user_id = :uid AND parcel_id = :raw_pid
      AND coalesce(btrim(property_address), '') IN ('', '(enrichment unavailable)')
      AND mailing_address IS NOT DISTINCT FROM :old_mail
      AND (enrichment_data IS NULL OR jsonb_typeof(enrichment_data::jsonb) IN ('object', 'null'))
      AND is_duplicate = false
      AND enrichment_data::jsonb->>'delivery_excluded_reason' IS NULL
      AND (CAST(:resolved_pin AS text) IS NULL
           OR coalesce(enrichment_data::jsonb->>'resolved_parcel_id', '') IN ('', :resolved_pin))
      -- The PIN the decision was made about is still the one this row looks up (a row
      -- this run resolves still looks up its raw parcel until the write lands).
      AND (CASE WHEN enrichment_data::jsonb->>'resolved_by' = 'rpacct_account_number'
                THEN enrichment_data::jsonb->>'resolved_parcel_id' ELSE btrim(parcel_id) END)
          = (CASE WHEN CAST(:resolved_pin AS text) IS NOT NULL THEN btrim(parcel_id)
                  ELSE CAST(:lookup_pin AS text) END)
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status = 'done')
"""


@dataclass(frozen=True)
class Decision:
    row: object
    lookup_pin: str | None
    outcome: str                 # fill_gis | fill_condo | unresolved:<why>
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    unit_nbr: str | None = None
    resolved_pin: str | None = None   # set only when THIS repair resolves an account number
    complex_pin: str | None = None


def lookup_pin(row, account_pins: dict[str, str]) -> tuple[str | None, str | None]:
    """(PIN to look up, PIN newly resolved here). Mirrors enrich._king_lookup_pin."""
    if row.resolved_by == KING_ACCOUNT_RESOLVER and _PIN_RE.fullmatch(row.resolved_pin or ""):
        return row.resolved_pin, None
    if _PIN_RE.fullmatch(row.pid):
        return row.pid, None
    if (_ACCOUNT_RE.fullmatch(row.pid) and row.row_source == "king_landmark_json"
            and not row.resolved_pin and row.pid in account_pins):
        return account_pins[row.pid], account_pins[row.pid]
    return None, None


def plan(rows, account_pins: dict[str, str], gis: dict[str, dict], units: dict) -> list[Decision]:
    """Decide every candidate. Pure: no I/O, so the rules are tested directly."""
    from src.scrapers.enrichment.king_condo_units import compose_fill, complex_pin

    decisions = []
    for row in rows:
        pin, newly = lookup_pin(row, account_pins)
        if pin is None:
            decisions.append(Decision(row, None, "unresolved:no_usable_pin"))
            continue
        direct = gis.get(pin) or {}
        if direct.get("property_address"):
            # The parcel's own GIS situs (a resolved account number, or a lookup the job
            # could not complete). A condo unit PIN never has one, so this cannot shadow it.
            decisions.append(Decision(
                row, pin, "fill_gis", address=direct["property_address"],
                city=direct.get("property_city") or direct.get("situs_city"),
                state=direct.get("property_state") or direct.get("situs_state"),
                zip=direct.get("property_zip") or direct.get("situs_zip"), resolved_pin=newly))
            continue
        situs = units.get(pin)
        if situs is None or situs.status == "absent":
            decisions.append(Decision(row, pin, "unresolved:no_gis_or_condo_situs", resolved_pin=newly))
            continue
        cpin = complex_pin(pin)
        fill = compose_fill(situs, gis.get(cpin))
        if fill is None:
            why = "no_locality" if situs.status == "found" else situs.status
            decisions.append(Decision(row, pin, f"unresolved:{why}", unit_nbr=situs.unit_nbr,
                                      resolved_pin=newly))
            continue
        decisions.append(Decision(row, pin, "fill_condo", address=fill.property_address,
                                  city=fill.city, state=fill.state, zip=fill.zip,
                                  unit_nbr=situs.unit_nbr, resolved_pin=newly, complex_pin=cpin))
    return decisions


def apply(db, decisions: list[Decision], snapshots: dict[str, str], *, commit_every: int = 200) -> Counter:
    from src.utils.address_intel import compute_owner_flags

    counts: Counter = Counter()
    now = datetime.now(UTC).isoformat()
    for i, d in enumerate([d for d in decisions if d.address], 1):
        row = d.row
        flags = compute_owner_flags(d.address, row.mailing_address,
                                    property_city=row.property_city or d.city,
                                    property_state=row.property_state or d.state,
                                    property_zip=row.property_zip or d.zip)
        patch: dict = {"property_repaired_at": now}
        if d.outcome == "fill_condo":
            patch.update({"property_source": REASON_CONDO, "property_source_snapshot": snapshots["condo"],
                          "property_locality_source": f"king_gis_complex:{d.complex_pin}",
                          "condo_unit_status": "found", "condo_unit_snapshot": snapshots["condo"],
                          "property_repair_reason": REASON_CONDO})
            if d.unit_nbr:
                patch["condo_unit_nbr"] = d.unit_nbr
        else:
            patch.update({"property_source": "king_gis",
                          "property_repair_reason": REASON_ACCOUNT if (d.resolved_pin or d.lookup_pin != row.pid)
                          else REASON_GIS})
        if d.resolved_pin:
            patch.update({"resolved_parcel_id": d.resolved_pin, "source_parcel_id": row.pid,
                          "resolved_by": KING_ACCOUNT_RESOLVER, "resolved_snapshot": snapshots["rpacct"]})
        result = db.execute(text(_UPDATE_SQL), {
            "address": d.address, "city": d.city, "state": d.state, "zip": d.zip,
            "f_owner_state": flags["owner_state"], "f_absentee": flags["absentee_owner"],
            "f_out_of_state": flags["out_of_state_owner"], "patch": json.dumps(patch),
            "rid": row.id, "uid": row.user_id, "raw_pid": row.raw_pid, "old_mail": row.mailing_address,
            "resolved_pin": d.resolved_pin, "lookup_pin": d.lookup_pin,
        })
        counts["written" if result.rowcount else "skipped_by_write_guard"] += 1
        if i % commit_every == 0:
            db.commit()
    db.commit()
    return counts


def run(db, rpacct_zip: Path, condo_zip: Path, snapshots: dict[str, str], *, record_type: str,
        job_id: str | None, apply_writes: bool, report: Path | None) -> dict:
    from src.scrapers.enrichment import county_gis
    from src.scrapers.enrichment.king_condo_units import complex_pin, load_units, unit_situs
    from src.scrapers.enrichment.king_rpacct import load_account_pins

    rows = db.execute(text(_CANDIDATES_SQL), {"record_type": record_type, "job_id": job_id}).all()
    db.rollback()  # release the read snapshot before the file scans and HTTP
    accounts = {r.pid for r in rows if _ACCOUNT_RE.fullmatch(r.pid) and r.row_source == "king_landmark_json"}
    account_pins = load_account_pins(rpacct_zip, accounts) if accounts else {}
    pins = {p for p in (lookup_pin(r, account_pins)[0] for r in rows) if p}
    units = {pin: unit_situs(v) for pin, v in load_units(condo_zip, pins).items()} if pins else {}
    ask = sorted(pins | {complex_pin(p) for p, s in units.items() if s.status == "found" and s.zip})
    gis = county_gis.batch_enrich_parcels_gis(ask, "king", "WA") if ask else {}
    decisions = plan(rows, account_pins, gis, units)

    stats = {"record_type": record_type, "snapshots": snapshots, "candidate_rows": len(rows),
             "candidate_jobs": len({str(r.job_id) for r in rows}),
             "by_outcome": dict(Counter(d.outcome for d in decisions))}
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for d in decisions:
                fh.write(json.dumps({
                    "result_id": str(d.row.id), "job_id": str(d.row.job_id), "parcel_id": d.row.pid,
                    "lookup_pin": d.lookup_pin, "outcome": d.outcome, "new_property_address": d.address,
                    "mailing_address": d.row.mailing_address,
                    "action": "write" if (apply_writes and d.address) else "none",
                }) + "\n")
    if apply_writes:
        stats["writes"] = dict(apply(db, decisions, snapshots))
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--record-type", default="pre_foreclosure")
    ap.add_argument("--job-id")
    ap.add_argument("--rpacct-zip", type=Path)
    ap.add_argument("--condo-zip", type=Path)
    ap.add_argument("--snapshot", help="extract date when both zips are given (YYYY-MM-DD)")
    ap.add_argument("--report", type=Path,
                    default=Path(f"repair_king_property_situs_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)

    from src.db.session import system_sync_session
    from src.scrapers.enrichment import king_condo_units, king_rpacct

    with tempfile.TemporaryDirectory() as tmp:
        snapshots = {"rpacct": args.snapshot or "unknown", "condo": args.snapshot or "unknown"}
        rpacct_zip, condo_zip = args.rpacct_zip, args.condo_zip
        if rpacct_zip is None:
            rpacct_zip = Path(tmp) / "rpacct.zip"
            snapshots["rpacct"] = king_rpacct.download_extract(rpacct_zip)
        if condo_zip is None:
            condo_zip = Path(tmp) / "condo.zip"
            snapshots["condo"] = king_condo_units.download_extract(condo_zip)
        with system_sync_session() as db:
            stats = run(db, rpacct_zip, condo_zip, snapshots, record_type=args.record_type,
                        job_id=args.job_id, apply_writes=args.apply, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
