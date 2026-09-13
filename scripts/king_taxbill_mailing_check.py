"""Ask King's live tax bill for the King mailing addresses the Assessor extract cannot answer.

The weekly Assessor extract (`scripts/king_rpacct_mailing.py`) repairs or fills most King
mailing addresses, but it has no single answer for some parcels: the PIN is absent from
the file, or its accounts disagree. Those rows are left for this script:

  echo       a pre-#210 situs echo (the property's own street copied into mailing)
  truncated  a pre-#289 tax-bill parse that lost its city, state and ZIP
  cv         a King code violation whose strictly located parcel (enrichment_data.kc_pin)
             has no extract answer, so it has no mailing address at all

Each parcel is looked up once through `batch_enrich_king_county`, the production path:
it shares the cross-process King source lease, stops on its circuit breaker, and only
reports "found" when the rendered tax bill names the parcel and ends in a postal code.
A page for a different or recovered parcel is never used. Pacing is several seconds per
page; King has IP-blocked faster traffic.

Writes (only with --apply) are guarded single-row UPDATEs on done jobs. The row must
still hold the exact mailing value (or NULL, for cv) and parcel identity it was selected
with and carry no mailing_source. "found" writes the address with
`mailing_source=king_tax_bill` and recomputes the owner flags; "none" (the county shows
no mailing address) stamps only `mailing_taxbill_outcome`. Anything else writes nothing,
so a later run tries it again. A stamped row is never selected again. Nothing touches
parcel_id, property_address, dedup, billing, quota or skip trace.

    railway run --service worker python scripts/king_taxbill_mailing_check.py            # dry-run
    railway run --service worker python scripts/king_taxbill_mailing_check.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

SOURCE = "king_tax_bill"
OUTCOME_KEY = "mailing_taxbill_outcome"
_CHUNK = 150  # batch_enrich_king_county caps one mailing pass at 200 pages

_CV_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.parcel_id AS raw_pid, r.enrichment_data::jsonb->>'kc_pin' AS pid,
           r.property_address, r.property_city, r.property_state, r.property_zip,
           r.mailing_address
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'code_violation'
      AND j.status = 'done'
      AND r.parcel_id IS NULL
      AND r.mailing_address IS NULL
      AND r.enrichment_data::jsonb->>'kc_pin_status' = 'matched'
      AND r.enrichment_data::jsonb->>'kc_pin' ~ '^[0-9]{10}$'
      AND btrim(coalesce(r.enrichment_data::jsonb->>'mailing_source', '')) = ''
      AND NOT (r.enrichment_data::jsonb ? 'mailing_taxbill_outcome')
    ORDER BY r.id
"""

_STAMPED_SQL = """
    SELECT r.id FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND r.enrichment_data::jsonb ? 'mailing_taxbill_outcome'
"""

_UPDATE_SQL = """
    UPDATE results SET
      mailing_address = CASE WHEN CAST(:new_mail AS text) IS NOT NULL
                             THEN CAST(:new_mail AS text) ELSE mailing_address END,
      property_state = CASE WHEN CAST(:new_mail AS text) IS NOT NULL
                            THEN CAST(:f_property_state AS varchar) ELSE property_state END,
      owner_state = CASE WHEN CAST(:new_mail AS text) IS NOT NULL
                         THEN CAST(:f_owner_state AS varchar) ELSE owner_state END,
      absentee_owner = CASE WHEN CAST(:new_mail AS text) IS NOT NULL
                            THEN CAST(:f_absentee AS boolean) ELSE absentee_owner END,
      out_of_state_owner = CASE WHEN CAST(:new_mail AS text) IS NOT NULL
                                THEN CAST(:f_out_of_state AS boolean) ELSE out_of_state_owner END,
      enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
                               THEN enrichment_data::jsonb ELSE '{}'::jsonb END)
                         || CAST(:payload AS jsonb))::json
    WHERE id = :rid
      AND user_id = :uid
      AND parcel_id IS NOT DISTINCT FROM :raw_pid
      AND mailing_address IS NOT DISTINCT FROM :old_mail
      AND (enrichment_data IS NULL OR jsonb_typeof(enrichment_data::jsonb) IN ('object', 'null'))
      AND btrim(coalesce(enrichment_data::jsonb->>'mailing_source', '')) = ''
      AND NOT coalesce(enrichment_data::jsonb ? 'mailing_taxbill_outcome', false)
      AND (CAST(:kind AS text) <> 'cv'
           OR coalesce(enrichment_data::jsonb->>'kc_pin', '') = CAST(:pid AS text))
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status = 'done')
"""


def _load_rpacct_script():
    path = Path(__file__).resolve().parent / "king_rpacct_mailing.py"
    spec = importlib.util.spec_from_file_location("king_rpacct_mailing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def select_candidates(db, answers_for) -> list[dict]:
    """Every row this script may look up: echo/truncated rows the extract left unresolved,
    plus located code violations without an extract answer. `answers_for(pins)` returns
    the extract's {pin: Answer}."""
    krm = _load_rpacct_script()
    stamped = {str(x) for x in db.execute(text(_STAMPED_SQL)).scalars()}
    parcel_rows = [r for r in db.execute(text(krm._CANDIDATES_SQL)).all() if str(r.id) not in stamped]
    cv_rows = db.execute(text(_CV_CANDIDATES_SQL)).all()
    db.rollback()
    answers = answers_for({r.pid for r in parcel_rows} | {r.pid for r in cv_rows})

    out = []
    for d in krm.plan(parcel_rows, answers):
        if d["pass"] in ("echo_unresolved", "truncated_unresolved"):
            out.append({"row": d["row"], "kind": d["pass"].split("_")[0],
                        "extract_status": d["status"]})
    for r in cv_rows:
        ans = answers.get(r.pid)
        if ans is None or ans.status != "found":
            out.append({"row": r, "kind": "cv", "extract_status": ans.status if ans else "absent"})
    return out


def decide(candidates: list[dict], looked_up: dict[str, dict]) -> list[dict]:
    """Pure: the write (if any) for each candidate given the tax-bill lookup results."""
    decisions = []
    for c in candidates:
        res = looked_up.get(c["row"].pid) or {}
        outcome = res.get("mailing_lookup") or "not_reached"
        trusted = res.get("parcel_lookup") in ("verified", "echo_absent")
        new_mail = res.get("mailing_address") if (outcome == "found" and trusted) else None
        if outcome == "found" and not trusted:
            outcome = "parcel_unverified"
        write = outcome == "none" and trusted or new_mail is not None
        decisions.append({**c, "outcome": outcome, "new_mail": new_mail, "write": write})
    return decisions


def lookup(pins: list[str], *, pace_s: float, budget_s: float) -> tuple[dict[str, dict], dict]:
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

    results: dict[str, dict] = {}
    stats: dict = {"chunks": []}
    for i in range(0, len(pins), _CHUNK):
        chunk = pins[i:i + _CHUNK]
        st: dict = {}
        results.update(asyncio.run(batch_enrich_king_county(
            chunk, time_budget_s=budget_s, stats=st, pace_s=pace_s)) or {})
        stats["chunks"].append({k: st.get(k) for k in (
            "requested", "mailing_attempted", "mailing_found", "budget_exhausted",
            "parcel_mismatch", "phase1_outcomes")})
        if st.get("budget_exhausted"):
            break  # a tripped breaker, a lost lease or a spent budget: stop, rerun later
    return results, stats


def apply(db, decisions: list[dict]) -> Counter:
    from src.utils.address_intel import compute_owner_flags

    counts: Counter = Counter()
    now = datetime.now(UTC).isoformat()
    for d in (x for x in decisions if x["write"]):
        r = d["row"]
        flags = compute_owner_flags(r.property_address, d["new_mail"], property_city=r.property_city,
                                    property_state=r.property_state, property_zip=r.property_zip)
        payload: dict = {OUTCOME_KEY: d["outcome"], "mailing_taxbill_checked_at": now}
        if d["new_mail"]:
            payload.update({"mailing_source": SOURCE, "mailing_lookup_deferred": False})
            if r.mailing_address:
                payload["mailing_repair_previous"] = r.mailing_address
                payload["mailing_repair_reason"] = (
                    "situs_echo" if d["kind"] == "echo" else "truncated_taxbill")
        res = db.execute(text(_UPDATE_SQL), {
            "new_mail": d["new_mail"], "rid": r.id, "uid": r.user_id, "raw_pid": r.raw_pid,
            "old_mail": r.mailing_address, "kind": d["kind"], "pid": r.pid,
            "payload": json.dumps(payload),
            "f_property_state": flags["property_state"], "f_owner_state": flags["owner_state"],
            "f_absentee": flags["absentee_owner"], "f_out_of_state": flags["out_of_state_owner"],
        })
        counts["written" if res.rowcount else "skipped_by_write_guard"] += 1
    db.commit()
    return counts


def run(db, answers_for, *, apply_writes: bool, report: Path | None,
        pace_s: float = 4.0, budget_s: float = 3 * 3600, limit: int | None = None) -> dict:
    candidates = select_candidates(db, answers_for)
    if limit is not None:
        candidates = candidates[:limit]
    pins = list(dict.fromkeys(c["row"].pid for c in candidates))
    looked_up, lookup_stats = lookup(pins, pace_s=pace_s, budget_s=budget_s) if pins else ({}, {})
    decisions = decide(candidates, looked_up)
    stats = {
        "candidates": dict(Counter(c["kind"] for c in candidates)),
        "parcels": len(pins),
        "outcomes": dict(Counter(f"{d['kind']}:{d['outcome']}" for d in decisions)),
        "would_write": sum(1 for d in decisions if d["write"]),
        "lookup": lookup_stats,
    }
    if report is not None:
        with report.open("w", encoding="utf-8") as fh:
            for d in decisions:
                fh.write(json.dumps({
                    "result_id": str(d["row"].id), "parcel_id": d["row"].pid, "kind": d["kind"],
                    "extract_status": d["extract_status"], "taxbill_outcome": d["outcome"],
                    "old_mailing": d["row"].mailing_address, "new_mailing": d["new_mail"],
                    "property_address": d["row"].property_address,
                    "action": ("write" if apply_writes and d["write"] else
                               "would_write" if d["write"] else "none"),
                }) + "\n")
    if apply_writes:
        stats["writes"] = dict(apply(db, decisions))
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, help="only the first N candidate rows")
    ap.add_argument("--pace", type=float, default=4.0, help="seconds between King page loads")
    ap.add_argument("--report", type=Path,
                    default=Path(f"king_taxbill_mailing_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")
    if args.pace < 2.0:
        ap.error("--pace must be at least 2 seconds (King has IP-blocked faster traffic)")

    from src.db.session import system_sync_session
    from src.scrapers.enrichment.king_rpacct import download_extract, load_accounts, resolve

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "rpacct.zip"
        snapshot = download_extract(zip_path)

        def answers_for(pins: set[str]) -> dict:
            accounts = load_accounts(zip_path, pins)
            return {p: resolve(accounts.get(p)) for p in pins}

        with system_sync_session() as db:
            stats = run(db, answers_for, apply_writes=args.apply, report=args.report,
                        pace_s=args.pace, limit=args.limit)
    stats["extract_snapshot"] = snapshot
    print(json.dumps(stats, indent=2, default=str))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
