"""Repair and fill King mailing addresses from the Assessor's bulk account extract.

Two passes over King County (WA) leads on terminal jobs:

  1. ECHO REPAIR. Before #210 the King tax scraper wrote the property address, with
     city/state/ZIP appended, into mailing_address ("506 S 330TH PL, FEDERAL WAY, WA
     98003-5900"). That is not a mailing address from any source, and for an absentee
     owner it is wrong: King bills parcel 1321400230 to a trustee in Grapevine, TX.
     A row is repaired only when it carries that exact signature (same street as the
     property, then ", CITY, WA ZIP") and nothing records a verified mailing source.
       extract has one address -> that address (even when it IS the property: then the
                                  county itself says the owner gets mail there)
       otherwise               -> left exactly as it is, and counted. Shape alone is not
                                  proof of provenance, so nothing is NULLed on it (Codex P1).
  2. DEFERRED FILL. Rows whose mailing lookup was deferred (King rate-limited the
     tax-bill page) get the extract's address when it has exactly one.

Every write is a guarded single-row UPDATE (id, user_id, parcel_id and the old mailing
value must all still match), stamps the source and the extract date, and recomputes the
owner-location flags from the new value. Nothing touches parcel_id, property_address,
dedup, billing, quota, skip-trace state or contact data, and no job is created.

    railway run --service worker python scripts/king_rpacct_mailing.py                 # dry-run
    railway run --service worker python scripts/king_rpacct_mailing.py --apply
    ... --zip path/to/Real Property Account.zip   (skip the download)

A JSON-lines evidence file records every candidate and decision before any write.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

REPAIR_REASON = "situs_echo"

# Sources that already mean "a real county mailing answer": never second-guessed here.
_VERIFIED = ("king_assessor_tax_bill", "king_rpacct")

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, btrim(r.parcel_id) AS pid, r.parcel_id AS raw_pid,
           r.property_address, r.property_city, r.property_state, r.property_zip,
           r.mailing_address,
           r.enrichment_data::jsonb->>'mailing_source' AS mailing_source,
           r.enrichment_data::jsonb->>'mailing_recovery_outcome' AS recovery_outcome,
           coalesce(r.enrichment_data::jsonb->>'mailing_lookup_deferred', '') = 'true' AS deferred
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
      AND j.status = 'done'
      AND r.parcel_id IS NOT NULL
      AND btrim(r.parcel_id) ~ '^[0-9]{10}$'
      AND (r.mailing_address IS NOT NULL
           OR coalesce(r.enrichment_data::jsonb->>'mailing_lookup_deferred', '') = 'true')
    ORDER BY r.id
"""

# enrichment_data merges into the existing value only when it is a JSON object: rows
# written with None store JSON null, and 'null'::jsonb || '{...}' builds an array.
_UPDATE_SQL = """
    UPDATE results SET
      mailing_address = :new_mail,
      property_state = CAST(:f_property_state AS varchar),
      owner_state = CAST(:f_owner_state AS varchar),
      absentee_owner = CAST(:f_absentee AS boolean),
      out_of_state_owner = CAST(:f_out_of_state AS boolean),
      enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
                               THEN enrichment_data::jsonb ELSE '{}'::jsonb END)
                         || CAST(:payload AS jsonb))::json
    WHERE id = :rid
      AND user_id = :uid
      AND parcel_id = :raw_pid
      AND mailing_address IS NOT DISTINCT FROM :old_mail
      -- Fail closed on metadata this merge could destroy: an array or scalar (rows an old
      -- null-merge bug already damaged) is skipped, never replaced (Codex P1).
      AND (enrichment_data IS NULL OR jsonb_typeof(enrichment_data::jsonb) IN ('object', 'null'))
      -- Eligibility re-asserted at write time, not only at read time (Codex P1).
      AND coalesce(enrichment_data::jsonb->>'mailing_source', '')
          NOT IN ('king_assessor_tax_bill', 'king_rpacct')
      AND coalesce(enrichment_data::jsonb->>'mailing_recovery_outcome', '') <> 'found'
      AND (CAST(:pass AS text) = 'echo_repair'
           OR (mailing_address IS NULL
               AND coalesce(enrichment_data::jsonb->>'mailing_lookup_deferred', '') = 'true'))
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status = 'done')
"""

_ECHO_TAIL_RE = re.compile(r",\s*[A-Z][A-Z .'\-]*,\s*WA\s+\d{5}(?:-\d{4})?\s*$", re.I)


def _street(address: str | None) -> str:
    head = (address or "").split(",")[0].upper()
    return " ".join(re.sub(r"[^A-Z0-9# ]", " ", head).split())


def is_situs_echo(row) -> bool:
    """The pre-#210 signature: the property's own street, then ", CITY, WA ZIP"."""
    mail = row.mailing_address
    if not mail or not row.property_address:
        return False
    if row.mailing_source in _VERIFIED or row.recovery_outcome == "found":
        return False
    return _street(mail) == _street(row.property_address) and bool(_ECHO_TAIL_RE.search(mail))


def plan(rows, answers) -> list[dict]:
    """Decide every candidate. Pure: no I/O, so the decision rules are tested directly."""
    decisions = []
    for row in rows:
        answer = answers.get(row.pid)
        if row.mailing_address is not None:
            if not is_situs_echo(row):
                continue
            if answer is not None and answer.status == "found":
                decisions.append({"row": row, "pass": "echo_repair", "status": answer.status,
                                  "new_mail": answer.mailing_address})
            else:
                decisions.append({"row": row, "pass": "echo_unresolved",
                                  "status": answer.status if answer else "absent",
                                  "new_mail": None})
        elif row.deferred and answer is not None and answer.status == "found":
            decisions.append({"row": row, "pass": "deferred_fill", "status": answer.status,
                              "new_mail": answer.mailing_address})
    return decisions


def apply(db, decisions, snapshot: str, *, commit_every: int = 500) -> Counter:
    from src.scrapers.enrichment.king_rpacct import SOURCE
    from src.utils.address_intel import compute_owner_flags

    counts: Counter = Counter()
    now = datetime.now(UTC).isoformat()
    writable = [d for d in decisions if d["new_mail"]]
    if len(decisions) > len(writable):
        counts["left_unchanged_unresolved"] = len(decisions) - len(writable)
    for i, d in enumerate(writable, 1):
        row = d["row"]
        flags = compute_owner_flags(
            row.property_address, d["new_mail"], property_city=row.property_city,
            property_state=row.property_state, property_zip=row.property_zip,
        )
        payload: dict = {"mailing_rpacct_checked_at": now, "mailing_rpacct_snapshot": snapshot,
                         "mailing_source": SOURCE, "mailing_lookup_deferred": False,
                         "mailing_recovery_outcome": "found"}
        if d["pass"] == "echo_repair":
            payload["mailing_repair_reason"] = REPAIR_REASON
            payload["mailing_repair_previous"] = row.mailing_address
        result = db.execute(text(_UPDATE_SQL), {
            "new_mail": d["new_mail"], "rid": row.id, "uid": row.user_id,
            "raw_pid": row.raw_pid, "old_mail": row.mailing_address, "pass": d["pass"],
            "payload": json.dumps(payload),
            "f_property_state": flags["property_state"], "f_owner_state": flags["owner_state"],
            "f_absentee": flags["absentee_owner"], "f_out_of_state": flags["out_of_state_owner"],
        })
        counts["written" if result.rowcount else "skipped_by_write_guard"] += 1
        if i % commit_every == 0:
            db.commit()
    db.commit()
    return counts


def run(db, zip_path: Path, snapshot: str, *, apply_writes: bool, report: Path | None) -> dict:
    from src.scrapers.enrichment.king_rpacct import load_accounts, resolve

    rows = db.execute(text(_CANDIDATES_SQL)).all()
    db.rollback()  # release the read snapshot before the long file scan
    accounts = load_accounts(zip_path, {r.pid for r in rows})
    answers = {pid: resolve(accounts.get(pid)) for pid in {r.pid for r in rows}}
    decisions = plan(rows, answers)

    summary: Counter = Counter()
    for d in decisions:
        outcome = "replace" if d["new_mail"] else "left_unchanged"
        same = d["new_mail"] is not None and d["row"].mailing_address is not None and (
            _street(d["new_mail"]) == _street(d["row"].mailing_address))
        summary[f"{d['pass']}:{outcome}{':county_confirms_property' if same else ''}"] += 1
    stats = {"snapshot": snapshot, "candidate_rows": len(rows), "decisions": len(decisions),
             "by_outcome": dict(summary)}
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for d in decisions:
                r = d["row"]
                fh.write(json.dumps({
                    "result_id": str(r.id), "parcel_id": r.pid, "pass": d["pass"],
                    "extract_status": d["status"], "old_mailing": r.mailing_address,
                    "new_mailing": d["new_mail"], "property_address": r.property_address,
                    "action": ("write" if apply_writes else "dry-run"),
                }) + "\n")
    if apply_writes:
        stats["writes"] = dict(apply(db, decisions, snapshot))
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--zip", type=Path, help="use an already-downloaded extract")
    ap.add_argument("--snapshot", help="extract date when --zip is given (YYYY-MM-DD)")
    ap.add_argument("--report", type=Path,
                    default=Path(f"king_rpacct_mailing_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)

    from src.db.session import system_sync_session
    from src.scrapers.enrichment.king_rpacct import download_extract

    with tempfile.TemporaryDirectory() as tmp:
        if args.zip:
            zip_path, snapshot = args.zip, args.snapshot or "unknown"
        else:
            zip_path = Path(tmp) / "rpacct.zip"
            snapshot = download_extract(zip_path)
        with system_sync_session() as db:
            stats = run(db, zip_path, snapshot, apply_writes=args.apply, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
