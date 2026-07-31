"""READ-ONLY: validate the candidate column semantics across ALL rows.

Candidate: c11 = billed-to-date, c12 = paid, c13 = still owed, c14 = full-year levy.
Invariant under test: c11 == c12 + c13  (to the cent).
Do not trust a handful of sample rows -- check all 327k.
"""
import collections
import httpx
from decimal import Decimal, InvalidOperation

H = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
URL = "https://www.snohomishcountywa.gov/DocumentCenter/View/149973/snohomish_tax_data_totals"


def dec(s):
    s = (s or "").strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


ok = bad = skipped = 0
examples = []
c14_eq_c11 = c14_eq_2x = c14_other = 0
owed_pos_by_year = collections.Counter()
real_prior_owed = 0

with httpx.Client(follow_redirects=True, timeout=300, headers=H) as c:
    with c.stream("GET", URL) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            f = line.rstrip("\r\n").split("|")
            if len(f) != 15:
                skipped += 1
                continue
            billed, paid, owed, levy = (dec(f[11]), dec(f[12]), dec(f[13]), dec(f[14]))
            if None in (billed, paid, owed):
                skipped += 1
                continue
            if billed == paid + owed:
                ok += 1
            else:
                bad += 1
                if len(examples) < 6:
                    examples.append(f[:2] + f[11:15])
            if levy is not None and billed:
                if levy == billed:
                    c14_eq_c11 += 1
                elif abs(levy - billed * 2) <= Decimal("0.02"):
                    c14_eq_2x += 1
                else:
                    c14_other += 1
            yr = f[1].strip()
            if owed > 0:
                owed_pos_by_year[yr] += 1
                if len(f[0].strip()) == 14 and yr.isdigit() and int(yr) < 2026:
                    real_prior_owed += 1

tot = ok + bad
print(f"rows tested: {tot}   skipped: {skipped}")
print(f"INVARIANT c11 == c12 + c13:  holds {ok} ({ok/tot:.4%})   fails {bad}")
for e in examples:
    print("   FAIL:", e)
print(f"\nc14 vs c11:  equal={c14_eq_c11}  ~2x={c14_eq_2x}  other={c14_other}")
print(f"\nrows with owed>0 by tax year: {dict(sorted(owed_pos_by_year.items()))}")
print(f"\n>>> 14-digit REAL-PROPERTY rows, prior-year (<2026), owed>0: {real_prior_owed}")
