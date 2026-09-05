"""Repair King tax-delinquent rows persisted by the pre-#210 defective logic.

WHAT WAS WRONG (all fixed at source in #210; this repairs rows already stored):

  date_recorded    - synthesized as "01/01/<bill_year>". The King Socrata feed is a
                     tax RECEIVABLE ROLL: it carries a bill YEAR and no filing,
                     recording or delinquency date. The value asserted an event on a
                     specific January 1st that never happened.
  delinquent_amount / delinquent_bill_year (and their enrichment_data twins)
                   - the Socrata query clipped bill_year to the job's date window, so
                     a parcel delinquent before the window had its balance understated
                     and its "oldest tax year" reported too recent. King's feed carries
                     bill_year back to 2002.
  mailing_address  - the situs echoed with city/state/ZIP appended. Not a taxpayer
                     mailing address at all; for an absentee owner it is actively
                     wrong, and it silently states an owner-occupancy no source gave us.

WHAT THIS DOES NOT TOUCH (identity / delivery / billing history):
  delivered_records, dedup_hash, source_fingerprint, billed_count, billing_applied_at,
  users.records_used, job.status, job.export_key, parcel_id, property_address.
  Correcting lead FACTS must not rewrite what was delivered or what was charged.

FAITHFULNESS
  The corrected values come from `aggregate_delinquent_rows` - the SAME function the
  fixed scraper uses - fed live Socrata rows for these parcels. It is deliberately not
  a reimplementation: a second copy of the money rules would be free to drift from the
  scraper (Codex P1), and a repair that disagrees with a fresh scrape is a new bug.
  That also means the selection window is applied exactly as the scraper applies it:
  a parcel whose in-window debt has since been paid is NOT emitted, and this script
  then leaves its money fields alone rather than inventing a full-history total for a
  parcel the scraper would no longer call a lead.

USAGE
    railway run python scripts/repair_king_tax_historical.py --job <uuid>
    railway run python scripts/repair_king_tax_historical.py --job <uuid> --expect 384 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.king_wa_tax_delinquent import aggregate_delinquent_rows  # noqa: E402

_API = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_PAGE = 1000
_CHUNK = 40
_PARCEL_RE = re.compile(r"^\d{10}$")

# enrichment_data keys the scraper derives from the money math. Repairing the typed
# columns without these would leave the API's enrichment_data disagreeing with them
# (ResultRow exposes both) - a fresh scrape and a repaired row must not differ.
_TAX_KEYS = (
    "delinquent_amount", "bill_year", "delinquent_years", "delinquent_year_count",
    "oldest_tax_year", "amount_by_charge_type", "amount_by_year", "account_numbers",
)


def _fetch_rows(parcels: list[str]) -> list[dict]:
    """Every Socrata charge line for these parcels, ALL years (no lower bound).

    The missing lower bound is the whole point: the aggregator needs the full history
    to compute a correct balance and oldest year.
    """
    out: list[dict] = []
    for i in range(0, len(parcels), _CHUNK):
        chunk = parcels[i:i + _CHUNK]
        where = " OR ".join(f"starts_with(account_number,'{p}')" for p in chunk)
        off = 0
        while True:
            r = requests.get(
                _API,
                params={"$where": f"({where})", "$limit": _PAGE, "$offset": off, "$order": ":id"},
                headers=_HEADERS,
                timeout=90,
            )
            r.raise_for_status()
            page = r.json()
            out.extend(page)
            off += _PAGE
            if len(page) < _PAGE:
                break
        print(f"  source: {min(i + _CHUNK, len(parcels))}/{len(parcels)} parcels", end="\r")
    print()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--expect", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--evidence-dir", default=".")
    args = ap.parse_args()

    with system_sync_session() as db:
        job = db.execute(
            text(
                "SELECT j.id::text id, j.user_id::text uid, j.status, j.date_from, j.date_to, "
                "sc.county, sc.state, sc.record_type "
                "FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
                "WHERE j.id = :j"
            ),
            {"j": args.job},
        ).first()
        if job is None:
            raise SystemExit(f"job {args.job} not found")
        if (job.county or "").lower() != "king" or (job.state or "").upper() != "WA" \
                or job.record_type != "tax_delinquent":
            raise SystemExit(
                f"refusing: job is {job.county}/{job.state}/{job.record_type}, "
                "not king/WA/tax_delinquent"
            )
        start_year = datetime.strptime(job.date_from, "%m/%d/%Y").year
        # Same cap the scraper applies, so a date_to in the future cannot pull a
        # not-yet-billed year (king_wa_tax_delinquent.scrape).
        end_year = min(datetime.strptime(job.date_to, "%m/%d/%Y").year, datetime.now(UTC).year)
        print(
            f"job {job.id}  {job.county}/{job.state}/{job.record_type}  status={job.status}  "
            f"selection window {start_year}-{end_year}"
        )

        rows = db.execute(
            text(
                "SELECT id::text id, parcel_id, date_recorded, delinquent_amount, "
                "delinquent_bill_year, property_address, mailing_address, "
                "enrichment_data::text ed "
                "FROM results WHERE job_id = :j AND user_id = CAST(:u AS uuid) "
                "ORDER BY parcel_id"
            ),
            {"j": job.id, "u": job.uid},
        ).all()
        print(f"rows: {len(rows)}")
        if args.expect is not None and len(rows) != args.expect:
            raise SystemExit(f"refusing: expected {args.expect} rows, found {len(rows)}")
        if not rows:
            return

        parcels = sorted({r.parcel_id for r in rows if r.parcel_id})
        # starts_with() on a SHORT parcel would prefix-match other parcels' accounts
        # and pollute the aggregate, so require the exact King PIN shape (Codex P2).
        bad = [p for p in parcels if not _PARCEL_RE.match(p)]
        if bad:
            raise SystemExit(f"refusing: {len(bad)} parcel_id(s) are not 10 digits: {bad[:5]}")

        print(f"fetching {len(parcels)} parcels from King Socrata (full history)...")
        raw = _fetch_rows(parcels)
        print(f"  {len(raw)} charge lines; aggregating with the scraper's own function")
        records, _stats = aggregate_delinquent_rows(
            raw, start_year=start_year, effective_end_year=end_year, cap_min_year=None
        )
        truth = {rec.parcel_id: rec for rec in records}
        print(f"  scraper emits {len(truth)}/{len(parcels)} of these parcels today")

        plan: list[tuple] = []
        not_emitted: list[str] = []
        for r in rows:
            rec = truth.get(r.parcel_id or "")
            if rec is None:
                # The scraper would not emit this parcel today (in-window debt paid).
                # Leave its money alone - only date/mailing are repaired.
                not_emitted.append(r.parcel_id)
            ed_new = rec.enrichment_data if rec else None
            new_amt = Decimal(ed_new["delinquent_amount"]) if ed_new else None
            new_yr = ed_new["oldest_tax_year"] if ed_new else None
            drop_mail = bool(
                r.mailing_address
                and r.property_address
                and r.mailing_address.strip().startswith(r.property_address.strip() + ",")
            )
            changes: dict[str, tuple] = {}
            if r.date_recorded is not None:
                changes["date_recorded"] = (r.date_recorded, None)
            if rec and (r.delinquent_amount is None or Decimal(r.delinquent_amount) != new_amt):
                changes["delinquent_amount"] = (str(r.delinquent_amount), str(new_amt))
            if rec and r.delinquent_bill_year != new_yr:
                changes["delinquent_bill_year"] = (r.delinquent_bill_year, new_yr)
            if drop_mail:
                changes["mailing_address"] = (r.mailing_address, None)
            # enrichment_data twins move with the columns, never independently.
            merged_ed = None
            if rec and ("delinquent_amount" in changes or "delinquent_bill_year" in changes):
                cur = json.loads(r.ed) if r.ed else {}
                merged_ed = dict(cur)
                for k in _TAX_KEYS:
                    if k in ed_new:
                        merged_ed[k] = ed_new[k]
                if merged_ed != cur:
                    changes["enrichment_data"] = ("(tax keys)", "(recomputed)")
            if changes:
                plan.append((r, new_amt, new_yr, merged_ed, changes))

        def _n(key: str) -> int:
            return sum(1 for _r, _a, _y, _e, c in plan if key in c)

        recovered = sum(
            Decimal(c["delinquent_amount"][1]) - Decimal(c["delinquent_amount"][0])
            for _r, _a, _y, _e, c in plan
            if "delinquent_amount" in c
        )
        print(f"\nrows to change: {len(plan)} of {len(rows)}")
        print(f"  fabricated date_recorded   -> NULL : {_n('date_recorded')}")
        print(f"  delinquent_amount corrected       : {_n('delinquent_amount')}"
              f"   (net ${recovered:,.2f})")
        print(f"  delinquent_bill_year corrected    : {_n('delinquent_bill_year')}")
        print(f"  enrichment_data tax keys resynced : {_n('enrichment_data')}")
        print(f"  fabricated mailing_address -> NULL: {_n('mailing_address')}")
        if not_emitted:
            print(f"  parcels the scraper no longer emits (money untouched): {len(not_emitted)}")

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        ev = os.path.join(args.evidence_dir, f"king_tax_repair_{job.id[:8]}_{stamp}.jsonl")
        with open(ev, "w", encoding="utf-8") as fh:
            for r, _a, _y, _e, changes in plan:
                fh.write(
                    json.dumps(
                        {
                            "result_id": r.id,
                            "parcel_id": r.parcel_id,
                            "changes": {
                                k: {"old": str(v[0]),
                                    "new": (None if v[1] is None else str(v[1]))}
                                for k, v in changes.items()
                            },
                        }
                    )
                    + "\n"
                )
        print(f"evidence: {ev}")

        if not args.apply:
            print("\nDRY RUN - nothing written. Re-run with --apply.")
            return

        applied = 0
        for r, new_amt, new_yr, merged_ed, changes in plan:
            # ONE static, fully-parameterised statement. Which columns move is carried
            # by the :do_* flags rather than by assembling SQL text, so there is no
            # string-built query and the whole thing is readable here.
            #
            # EVERY column being written re-checks its OLD value in the WHERE clause
            # (IS NOT DISTINCT FROM handles NULL == NULL). If a row changed under us
            # between the dry run and the apply it is SKIPPED, not overwritten.
            applied += db.execute(
                text(
                    """
                    UPDATE results SET
                      date_recorded = CASE WHEN CAST(:do_date AS boolean)
                                           THEN NULL ELSE date_recorded END,
                      delinquent_amount = CASE WHEN CAST(:do_amt AS boolean)
                                               THEN :amt ELSE delinquent_amount END,
                      delinquent_bill_year = CASE WHEN CAST(:do_yr AS boolean)
                                                  THEN :yr ELSE delinquent_bill_year END,
                      mailing_address = CASE WHEN CAST(:do_mail AS boolean)
                                             THEN NULL ELSE mailing_address END,
                      enrichment_data = CASE WHEN CAST(:do_ed AS boolean)
                                             THEN CAST(:ed AS json) ELSE enrichment_data END
                    WHERE id = CAST(:id AS uuid)
                      AND user_id = CAST(:u AS uuid)
                      AND job_id = :j
                      AND (NOT CAST(:do_date AS boolean)
                           OR date_recorded IS NOT DISTINCT FROM :old_date)
                      AND (NOT CAST(:do_mail AS boolean)
                           OR mailing_address IS NOT DISTINCT FROM :old_mail)
                      AND (NOT CAST(:do_amt AS boolean)
                           OR delinquent_amount IS NOT DISTINCT FROM CAST(:old_amt AS numeric))
                      AND (NOT CAST(:do_yr AS boolean)
                           OR delinquent_bill_year IS NOT DISTINCT FROM CAST(:old_yr AS integer))
                    """
                ),
                {
                    "id": r.id, "u": job.uid, "j": job.id,
                    "do_date": "date_recorded" in changes,
                    "old_date": changes.get("date_recorded", (None, None))[0],
                    "do_amt": "delinquent_amount" in changes, "amt": new_amt,
                    "old_amt": r.delinquent_amount,
                    "do_yr": "delinquent_bill_year" in changes, "yr": new_yr,
                    "old_yr": r.delinquent_bill_year,
                    "do_mail": "mailing_address" in changes,
                    "old_mail": changes.get("mailing_address", (None, None))[0],
                    "do_ed": "enrichment_data" in changes,
                    "ed": json.dumps(merged_ed) if merged_ed is not None else None,
                },
            ).rowcount

        # A guard mismatch means the data moved under us. Commit nothing rather than
        # leave the job half-repaired (Codex P2).
        if applied != len(plan):
            db.rollback()
            raise SystemExit(
                f"ABORTED: {applied} of {len(plan)} rows matched their old-value guards "
                "- data changed since the dry run. Nothing was written; re-run."
            )
        db.commit()
        print(f"\napplied to {applied} row(s); all old-value guards matched")


if __name__ == "__main__":
    main()
