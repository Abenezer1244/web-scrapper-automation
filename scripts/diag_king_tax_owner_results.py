"""Quick API-only check of King tax_delinquent owner-name fix (PR #80).

Does NOT trigger a new job. Looks at jobs already running/finished this session,
pulls whatever results exist, and classifies party_name as real owner vs the
"Tax Delinquent — $X owed (Parcel …)" placeholder. Partial results are fine —
even a handful of real owner names proves the enrichment swap is live.

Run:
  BRIDGELEADS_ADMIN_PASSWORD=... python scripts/diag_king_tax_owner_results.py [JOB_ID ...]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests as req

from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party

API = os.getenv("BRIDGELEADS_API", "https://api.bridgeleads.io")
EMAIL = os.getenv("BRIDGELEADS_ADMIN_EMAIL", "admin@bridgeleads.io")
PASSWORD = os.environ["BRIDGELEADS_ADMIN_PASSWORD"]

# Jobs triggered this session (most-enriched first).
DEFAULT_JOBS = [
    "20b1017d-c3ee-44f3-bd63-342cea698b0f",  # first attempt (longest running)
    "c0aa9b6d-933c-4878-b972-025f8c433d01",  # second attempt
]


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def login() -> str:
    r = req.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    if r.status_code != 200:
        sys.exit(f"LOGIN FAILED {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("mfa_required"):
        sys.exit("LOGIN needs MFA code.")
    print(f"  login OK ({EMAIL})")
    return data["access_token"]


def job_status(tok, jid):
    r = req.get(f"{API}/jobs/{jid}", headers=_hdr(tok), timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


def fetch_results(tok, jid, cap=2000):
    rows, page, per = [], 1, 200
    while len(rows) < cap:
        r = req.get(f"{API}/jobs/{jid}/results", headers=_hdr(tok),
                    params={"page": page, "per_page": per}, timeout=30)
        if r.status_code != 200:
            # Fail loud — a swallowed fetch error must not read as "no results"
            # and let a partial/empty page pass as a verdict.
            sys.exit(f"RESULTS FETCH FAILED {r.status_code}: {r.text[:160]}")
        data = r.json()
        batch = data.get("results") or data.get("items") or data.get("records") or []
        rows.extend(batch)
        total = data.get("total")
        if not batch or (total is not None and len(rows) >= total) or len(batch) < per:
            break
        page += 1
    return rows


def classify(rows):
    """Split party_names into real owners, placeholders, and blank/invalid.

    A real owner is a NON-EMPTY string that is not the placeholder. None / "" /
    missing party_name is tracked as `blank` — never counted as a real owner, so a
    job full of empty names can't read as a PASS.
    """
    owners, placeholders, blank = [], [], []
    for x in rows:
        pn = x.get("party_name")
        if is_tax_placeholder_party(pn):
            placeholders.append(pn)
        elif isinstance(pn, str) and pn.strip():
            owners.append(pn)
        else:
            blank.append(pn)
    return owners, placeholders, blank


def main():
    print("=" * 64)
    print("King tax_delinquent owner-name — API result check (PR #80)")
    print("=" * 64)
    tok = login()
    jids = sys.argv[1:] or DEFAULT_JOBS
    for jid in jids:
        print(f"\n--- job {jid} ---")
        st = job_status(tok, jid)
        if st is None:
            print("  (no longer queryable / not found)")
            continue
        print(f"  status={st.get('status')}  record_count={st.get('record_count')}")
        rows = fetch_results(tok, jid)
        if not rows:
            print("  no results persisted yet (leads likely written at job completion)")
            continue
        owners, placeholders, blank = classify(rows)
        print(f"  results sampled : {len(rows)}")
        print(f"  REAL owner names: {len(owners)}")
        print(f"  placeholders    : {len(placeholders)}")
        print(f"  blank/invalid   : {len(blank)}")
        if owners:
            print("  sample REAL owners (fix is live):")
            for nm in owners[:12]:
                print(f"    • {nm}")
        if placeholders:
            print("  sample placeholders (un-enriched/capped/missed):")
            for nm in placeholders[:3]:
                print(f"    • {nm}")


if __name__ == "__main__":
    main()
