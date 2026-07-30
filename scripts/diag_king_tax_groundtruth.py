"""READ-ONLY: compute tax-filter ground-truth counts for a job, for the UI test.

Mirrors what `/jobs/{id}/results` returns: the 18-month cap is ALWAYS applied for
a tax-delinquent job, then the active amount/months filters. Counts are computed
with independent raw SQL (not the ORM filter builder) so agreement with the API
total is a real cross-check. The month->year math reuses the unit-tested pure
helpers in tax_filters so the cap/filter can't drift.

    railway run --service worker python scripts/diag_king_tax_groundtruth.py <job_id>
"""
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.api.tax_filters import bill_year_bounds_for_months, tax_cap_min_year
from src.db.session import system_sync_session

# Same combo shapes as the Snohomish UI test (label, filters).
COMBOS = [
    ("no filter", {}),
    ("min $ >= 5000", {"min_amount": 5000}),
    ("min $ >= 10000", {"min_amount": 10000}),
    ("max $ <= 1000", {"max_amount": 1000}),
    ("amount 1000..5000", {"min_amount": 1000, "max_amount": 5000}),
    ("max months <= 12", {"max_months": 12}),
    ("min months >= 24", {"min_months": 24}),
    ("min months >= 6", {"min_months": 6}),
]


def _count(db, job_id, cap_min_year, f) -> int:
    where = ["job_id = :job", "(delinquent_bill_year IS NULL OR delinquent_bill_year >= :cap)"]
    params = {"job": job_id, "cap": cap_min_year}
    if "min_amount" in f:
        where.append("delinquent_amount >= :min_amount")
        params["min_amount"] = f["min_amount"]
    if "max_amount" in f:
        where.append("delinquent_amount <= :max_amount")
        params["max_amount"] = f["max_amount"]
    if "min_months" in f or "max_months" in f:
        max_year, min_year = bill_year_bounds_for_months(
            f.get("min_months"), f.get("max_months"), today
        )
        if max_year is not None:
            where.append("delinquent_bill_year <= :max_year")
            params["max_year"] = max_year
        if min_year is not None:
            where.append("delinquent_bill_year >= :min_year")
            params["min_year"] = min_year
    sql = "SELECT count(*) FROM results WHERE " + " AND ".join(where)  # noqa: S608 — fixed literals + bound params
    return db.execute(text(sql), params).scalar()


today = datetime.now(UTC).date()


def main() -> None:
    job_id = sys.argv[1]
    cap = tax_cap_min_year(today)
    base = today.year * 12 + (today.month - 1)
    with system_sync_session() as db:
        total_rows = db.execute(
            text("SELECT count(*) FROM results WHERE job_id = :job"), {"job": job_id}
        ).scalar()
        print(f"job={job_id} today={today} base={base} cap_min_year={cap} total_rows={total_rows}")
        print("bill_year histogram (capped scope):")
        for y, n in db.execute(
            text(
                "SELECT delinquent_bill_year AS y, count(*) AS n FROM results "
                "WHERE job_id = :job GROUP BY delinquent_bill_year ORDER BY y"
            ),
            {"job": job_id},
        ).fetchall():
            print(f"   bill_year={y}: {n}")
        amt = db.execute(
            text(
                "SELECT min(delinquent_amount), max(delinquent_amount), "
                "count(delinquent_amount) FROM results WHERE job_id = :job"
            ),
            {"job": job_id},
        ).first()
        print(f"amount: min={amt[0]} max={amt[1]} non_null={amt[2]}")
        print("\nGROUND TRUTH (cap always applied) — paste into the King COMBOS:")
        for label, f in COMBOS:
            print(f"   {label:20} -> {_count(db, job_id, cap, f)}")


if __name__ == "__main__":
    main()
