"""Live API test: King tax-delinquent FILTER correctness on prod.

Mints an admin token (same path as trigger_king_scrape) and calls
GET /jobs/{KING_JOB}/results with each tax-filter combo, asserting the response
`total` matches an independently-computed DB ground truth (the deployed 18-month
cap + build_tax_conditions math, cross-checked via diag_king_tax_groundtruth.py).

This is the same assertion the headed ui_test_tax_filters.py makes (it intercepts
the same /results `total`); done here at the API layer so it needs no browser/MFA.
The UI-render path was already validated on Snohomish.

    railway run --service worker python scripts/api_test_king_tax_filters.py
"""
import sys

sys.path.insert(0, ".")

import requests  # noqa: E402

from src.api.auth import create_secure_token  # noqa: E402

API = "https://api.bridgeleads.io"
ADMIN_USER = "b6d2095d-d33c-4d46-841f-4f835032f563"
KING_JOB = "1a54d04e-07e8-46aa-9bfb-fe4f2df4d463"

# (label, query params, expected total) — expected from diag_king_tax_groundtruth.py
COMBOS = [
    ("no filter",          {},                                     24708),
    ("min $ >= 5000",      {"min_amount": 5000},                   15411),
    ("min $ >= 10000",     {"min_amount": 10000},                   7285),
    ("max $ <= 1000",      {"max_amount": 1000},                    3936),
    ("amount 1000..5000",  {"min_amount": 1000, "max_amount": 5000}, 5361),
    ("max months <= 12",   {"max_months": 12},                     21737),
    ("min months >= 24",   {"min_months": 24},                         0),
    ("min months >= 6",    {"min_months": 6},                       2971),
]


def main() -> None:
    token = create_secure_token(user_id=ADMIN_USER, amr=["pwd"])
    h = {"Authorization": f"Bearer {token}"}
    fails = 0
    print(f"job={KING_JOB}")
    for label, params, expected in COMBOS:
        q = {**params, "page": 1, "page_size": 1}
        r = requests.get(f"{API}/jobs/{KING_JOB}/results", headers=h, params=q, timeout=60)
        if r.status_code != 200:
            print(f"  [ERROR] {label:20} HTTP {r.status_code}: {r.text[:120]}")
            fails += 1
            continue
        total = r.json().get("total")
        ok = total == expected
        fails += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:20} API total={total!s:>6}  expected={expected}")
    print("=" * 60)
    print(f"RESULT: {'ALL PASS' if fails == 0 else str(fails) + ' FAIL(S)'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
