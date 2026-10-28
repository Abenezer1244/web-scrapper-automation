"""Read-only dry run: how every delivered code-violation run measures against the strict policy.

    railway run --service worker python scripts/cv_qualification_report.py [--json]

The owner decides whether code violations billed before the qualification gate (#542,
2026-10-27) are credited. This prints the numbers that decision needs, per account and
run, and changes nothing: one REPEATABLE READ, READ ONLY transaction is opened before
the first query, so Postgres itself refuses any write, and the session is closed without
a commit. It sends no alert, enqueues nothing, and prints counts only: no owner name, no
address, no case number. Accounts appear as a short id prefix (lengthened if two collide).

Per run:
  * billed (stored): jobs.billed_count, what the run charged. Never recomputed.
  * candidates now: non-duplicate rows with an address and no delivery exclusion, i.e. the
    rows the run's file would carry today. Can differ from billed: later sweeps fill
    addresses, purges remove rows; the difference is printed, never explained away.
  * strict now: those candidates judged by the code-violation policy as it stands
    (src/api/lead_qualification.py). A row from before the gate carries no policy stamp,
    so it is judged on an in-memory copy stamped for the purpose; nothing is written.
    Reasons overlap (a row can lack both owner and mailing); "failing" counts rows once.
  * named after delivery: failing or passing rows whose owner the background sweep named
    (cv_owner_recovery_outcome = found), i.e. incomplete when delivered, repaired later.
  * held: rows the gate held (post-gate runs), and how many are complete now (the next
    run delivers those).
  * already delivered: prior-run copies, and how many point at a source run that is gone.

The 929 in tasks/todo-cv-qualification.md is a 2026-10-25 measurement, not an expected
total: owner names filled since then move rows from failing to passing.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, EXCLUSION_REASONS, NOT_QUALIFIED, has_address
from src.api.lead_qualification import OWNER_SOURCE_BY_SOURCE, qualification_reasons, stamp
from src.workers.qualification_quality import GATE_DEPLOYED_AT

_READ_ONLY = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

_JOBS_SQL = """
SELECT j.id::text AS id, j.user_id::text AS user_id, j.started_at, j.finished_at,
       j.billed_count, j.billing_applied_at, j.breakdown_new, j.breakdown_not_qualified
FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE sc.record_type = 'code_violation' AND j.status = 'done'
ORDER BY j.finished_at
"""

# The columns the policy reads, for every row of those jobs. Values stay in memory.
_ROWS_SQL = """
SELECT r.job_id::text AS job_id, r.party_name, r.parcel_id, r.property_address,
       r.mailing_address, r.date_recorded, r.enrichment_data, r.is_duplicate,
       r.duplicate_reason, r.duplicate_source_job_id::text AS source_job_id
FROM results r JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id
JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE sc.record_type = 'code_violation' AND j.status = 'done'
"""

_EXISTING_JOBS_SQL = "SELECT id::text FROM jobs WHERE id = ANY(CAST(:ids AS uuid[]))"

_SOURCES = frozenset(OWNER_SOURCE_BY_SOURCE)


def _jurisdiction(ed: Any) -> str:
    source = ed.get("source") if isinstance(ed, dict) else None
    return source if source in _SOURCES else "other"


def _excluded(ed: Any) -> bool:
    return isinstance(ed, dict) and ed.get(DELIVERY_EXCLUDED_KEY) in EXCLUSION_REASONS


def _strict_reasons(row: dict) -> list[str]:
    """The policy's reasons for this row, judged as if stamped (a copy; never written)."""
    return qualification_reasons({**row, "enrichment_data": stamp(row["enrichment_data"])})


def short_ids(ids: set[str]) -> dict[str, str]:
    """A prefix per account, lengthened until no two accounts share one."""
    for width in (8, 12, 16, 36):
        out = {i: i[:width] for i in ids}
        if len(set(out.values())) == len(ids):
            return out
    return {i: i for i in ids}


def _run(job: dict) -> dict:
    return {
        "job": job["id"][:8], "account": job["user_id"],
        "finished": job["finished_at"].date().isoformat() if job["finished_at"] else None,
        "gate": "post" if job["started_at"] and job["started_at"] >= GATE_DEPLOYED_AT else "pre",
        "billed": int(job["billed_count"] or 0),
        "candidates": 0, "strict_pass": 0, "strict_fail": 0, "reasons": Counter(),
        "named_after_delivery": 0, "held": 0, "held_complete_now": 0,
        "prior_run_copies": 0, "copies_source_gone": 0,
        "by_jurisdiction": defaultdict(lambda: {"candidates": 0, "strict_fail": 0}),
    }


def build_report(db) -> dict:
    """Measure every done code-violation run. ``db`` must not have begun a transaction."""
    db.execute(text(_READ_ONLY))
    jobs = [dict(r) for r in db.execute(text(_JOBS_SQL)).mappings()]
    runs = {j["id"]: _run(j) for j in jobs}
    copy_sources: dict[str, list[str]] = defaultdict(list)
    for row in db.execute(text(_ROWS_SQL)).mappings():
        row = dict(row)
        run = runs.get(row["job_id"])
        if run is None:
            continue
        ed = row["enrichment_data"]
        if row["is_duplicate"]:
            if (row["duplicate_reason"] or "prior_run") == "prior_run":
                run["prior_run_copies"] += 1
                copy_sources[row["job_id"]].append(row["source_job_id"])
            continue
        if isinstance(ed, dict) and ed.get(DELIVERY_EXCLUDED_KEY) == NOT_QUALIFIED:
            run["held"] += 1
            if not _strict_reasons(row):
                run["held_complete_now"] += 1
            continue
        if _excluded(ed) or not has_address(row):
            continue
        reasons = _strict_reasons(row)
        place = run["by_jurisdiction"][_jurisdiction(ed)]
        run["candidates"] += 1
        place["candidates"] += 1
        if reasons:
            run["strict_fail"] += 1
            place["strict_fail"] += 1
            run["reasons"].update(reasons)
        else:
            run["strict_pass"] += 1
        if isinstance(ed, dict) and ed.get("cv_owner_recovery_outcome") == "found":
            run["named_after_delivery"] += 1
    sources = {s for ids in copy_sources.values() for s in ids if s}
    existing = {r[0] for r in db.execute(text(_EXISTING_JOBS_SQL), {"ids": sorted(sources)})} if sources else set()
    for job_id, ids in copy_sources.items():
        runs[job_id]["copies_source_gone"] = sum(1 for s in ids if not s or s not in existing)
    return _summarise(list(runs.values()))


def _summarise(runs: list[dict]) -> dict:
    names = short_ids({r["account"] for r in runs})
    accounts: dict[str, dict] = {}
    for r in runs:
        r["account"] = names[r["account"]]
        r["reasons"] = dict(r["reasons"].most_common())
        r["by_jurisdiction"] = {k: dict(v) for k, v in sorted(r["by_jurisdiction"].items())}
        a = accounts.setdefault(r["account"], {
            "runs": 0, "billed_pre_gate": 0, "candidates_pre_gate": 0, "strict_fail_pre_gate": 0,
            "named_after_delivery_pre_gate": 0, "billed_post_gate": 0, "held_post_gate": 0,
            "held_complete_now": 0})
        a["runs"] += 1
        if r["gate"] == "pre":
            a["billed_pre_gate"] += r["billed"]
            a["candidates_pre_gate"] += r["candidates"]
            a["strict_fail_pre_gate"] += r["strict_fail"]
            a["named_after_delivery_pre_gate"] += r["named_after_delivery"]
        else:
            a["billed_post_gate"] += r["billed"]
            a["held_post_gate"] += r["held"]
            a["held_complete_now"] += r["held_complete_now"]
    return {"gate_deployed_at": GATE_DEPLOYED_AT.isoformat(), "runs": runs, "accounts": accounts}


def render(report: dict) -> str:
    out = [f"Code-violation runs vs the strict policy (gate deployed {report['gate_deployed_at']}). "
           "Read only; counts only.", ""]
    head = (f"{'account':<10}{'job':<10}{'finished':<12}{'gate':<6}{'billed':>7}{'cands':>7}"
            f"{'pass':>6}{'fail':>6}{'named*':>7}{'held':>6}{'held ok':>8}{'copies':>7}{'gone':>6}")
    out.append(head)
    for r in report["runs"]:
        out.append(f"{r['account']:<10}{r['job']:<10}{r['finished'] or '':<12}{r['gate']:<6}"
                   f"{r['billed']:>7}{r['candidates']:>7}{r['strict_pass']:>6}{r['strict_fail']:>6}"
                   f"{r['named_after_delivery']:>7}{r['held']:>6}{r['held_complete_now']:>8}"
                   f"{r['prior_run_copies']:>7}{r['copies_source_gone']:>6}")
        if r["reasons"]:
            out.append("    fail reasons (overlap): " + ", ".join(f"{k} {v}" for k, v in r["reasons"].items()))
        for place, c in r["by_jurisdiction"].items():
            out.append(f"    {place}: {c['candidates']} candidates, {c['strict_fail']} fail")
    out += ["", "* named: owner named by the background sweep after the run delivered.", "",
            "Per account (pre-gate = the quota-credit question; nothing is changed automatically):"]
    for name, a in sorted(report["accounts"].items()):
        out.append(f"  {name}: {a['runs']} runs; pre-gate billed {a['billed_pre_gate']}, "
                   f"{a['candidates_pre_gate']} candidates now, {a['strict_fail_pre_gate']} fail strict now, "
                   f"{a['named_after_delivery_pre_gate']} named after delivery; post-gate billed "
                   f"{a['billed_post_gate']}, held {a['held_post_gate']} ({a['held_complete_now']} complete now)")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args()
    from src.db.session import system_sync_session

    try:
        with system_sync_session() as db:
            report = build_report(db)
            db.rollback()
    except Exception as exc:  # noqa: BLE001 -- never echo rows or bound values
        print(f"Report failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
