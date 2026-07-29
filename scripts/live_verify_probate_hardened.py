"""SAFE non-persisting live PROBATE verification with party-orientation assertions.

Verification only. Resolves each county's probate scraper via the registry
(get_scraper_class -> scrape) — NO Celery, NO DB writes, NO enrichment, NO export,
NO billing. Then ASSERTS data quality, not just count:
  - party_name must look like a PERSON (the decedent), NOT a filing agency
    (Dept of Health/Licensing), a court/clerk, a filing state ("WASH STATE OF"),
    or an org/company. Those are RED FLAGS counted + sampled per county.
  - empty party_name is counted.

Run BY ABSOLUTE PATH so the worktree src wins:
  PYTHONPATH="<worktree>" railway run --service api -- \
    python "<worktree>/scripts/live_verify_probate_hardened.py" [--days N] [--conc K] [--only c1,c2]

Output: scripts/audit_out/probate_live_hardened.json (+ progress to stdout).
"""

import argparse
import asyncio
import inspect
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from src.scrapers.registry import get_scraper_class

_COUNTIES = [
    "benton", "chelan", "clallam", "clark", "columbia", "cowlitz", "douglas",
    "grant", "island", "jefferson", "king", "kitsap", "lewis", "okanogan",
    "pacific", "pierce", "skagit", "spokane", "thurston", "whatcom", "whitman",
]
_STATE = "wa"
_RECORD_TYPE = "probate"
_OUT = Path("scripts/audit_out/probate_live_hardened.json")

# party_name RED FLAGS — a probate lead's party should be the DECEDENT (a person).
# Each tuple: (flag_label, compiled regex over the UPPERCASED party_name).
_RED_FLAGS = [
    ("filing_agency", re.compile(r"\bDEP(AR)?T(MENT)?\.?\s+OF\s+(HEALTH|LICENSING|REVENUE|SOCIAL)")),
    ("dept_of_health", re.compile(r"\bDEPARTMENT\s+OF\s+HEALTH\b|\bDOH\b")),
    ("court_clerk", re.compile(r"\b(SUPERIOR|DISTRICT)\s+COURT\b|\bCOUNTY\s+CLERK\b|\bCLERK\s+OF\b")),
    # filing state as the WHOLE party, e.g. "WASH. STATE OF" / "WASHINGTON STATE OF" / "CALIFORNIA STATE OF"
    ("filing_state", re.compile(r"^\s*(WASH(INGTON)?\.?|CALIFORNIA|OREGON|IDAHO|STATE)\s+STATE\s+OF\s*$")),
    ("bare_state", re.compile(r"^\s*STATE\s+OF\s+\w+\s*$")),
    ("estate_caption", re.compile(r"^\s*ESTATE\s+OF\b")),
    # org/company tokens — decedent should be a person, not a company
    ("org_company", re.compile(r"\b(LLC|L\.?L\.?C|INC\b|INCORPORATED|CORP\b|CORPORATION|COMPANY|"
                               r"\bBANK\b|TITLE\s+CO|ESCROW|TRUSTEE|ASSOCIATION|N\.?A\.?$)\b")),
]


def _party_red_flag(party: str | None) -> str | None:
    if not party or not party.strip():
        return "EMPTY"
    s = party.upper()
    for label, rx in _RED_FLAGS:
        if rx.search(s):
            return label
    return None


def _summarize(county: str, records) -> dict:
    doc_types = Counter((r.doc_type or "<none>") for r in records)
    n = len(records)
    with_party = sum(1 for r in records if (r.party_name or "").strip())
    with_date = sum(1 for r in records if (r.date_recorded or "").strip())
    with_parcel = sum(1 for r in records if (r.parcel_id or "").strip())

    bad = []  # records whose party_name trips a red flag
    for r in records:
        flag = _party_red_flag(r.party_name)
        if flag:
            bad.append({"flag": flag, "party_name": (r.party_name or "")[:70],
                        "doc_type": r.doc_type, "heirs": (r.heirs or "")[:70]})
    bad_counter = Counter(b["flag"] for b in bad)

    samples = []
    for r in records[:5]:
        samples.append({
            "doc_type": r.doc_type,
            "party_name": (r.party_name or "")[:70],
            "heirs": (r.heirs or "")[:70],
            "date_recorded": r.date_recorded,
            "parcel_id": r.parcel_id,
        })
    return {
        "county": county,
        "ok": True,
        "count": n,
        "party_health": {
            "with_party": with_party,
            "empty_party": n - with_party,
            "red_flag_total": len(bad),
            "red_flags": dict(bad_counter),
            "bad_samples": bad[:6],
        },
        "doc_types": dict(doc_types),
        "fill": {"date_recorded": with_date, "parcel_id": with_parcel},
        "samples": samples,
    }


async def run_county(county: str, df: str, dt: str, sem: asyncio.Semaphore, timeout: int) -> dict:
    async with sem:
        try:
            factory, rt = get_scraper_class(county, _STATE, _RECORD_TYPE)
        except Exception as exc:
            print(f"[{county}] REGISTRY ERROR: {type(exc).__name__}: {exc}", flush=True)
            return {"county": county, "ok": False, "error": f"registry: {exc}"}

        kwargs = {}
        try:
            if "record_type" in inspect.signature(factory).parameters:
                kwargs["record_type"] = rt
        except (ValueError, TypeError):
            pass

        try:
            async with factory(**kwargs) as scraper:
                records = await asyncio.wait_for(scraper.scrape(df, dt), timeout=timeout)
            res = _summarize(county, records)
            ph = res["party_health"]
            print(
                f"[{county}] OK count={res['count']} "
                f"red_flags={ph['red_flag_total']}({ph['red_flags']}) "
                f"empty={ph['empty_party']} doc_types={list(res['doc_types'])[:4]}",
                flush=True,
            )
            return res
        except TimeoutError:
            print(f"[{county}] TIMEOUT after {timeout}s", flush=True)
            return {"county": county, "ok": False, "error": f"timeout_{timeout}s"}
        except Exception as exc:
            print(f"[{county}] SCRAPE ERROR: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            return {"county": county, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def main(days: int, conc: int, only: list[str] | None, timeout: int) -> None:
    date_to = datetime.now()
    date_from = date_to - timedelta(days=days)
    df, dt = date_from.strftime("%m/%d/%Y"), date_to.strftime("%m/%d/%Y")
    counties = sorted(set(only or _COUNTIES))
    print(f"Probate live verify (hardened): {len(counties)} counties, window {df} -> {dt}, conc={conc}", flush=True)
    sem = asyncio.Semaphore(conc)
    results = await asyncio.gather(*[run_county(c, df, dt, sem, timeout) for c in counties])
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps({"window": [df, dt], "results": results}, indent=2))
    ok = [r for r in results if r.get("ok")]
    flagged = [r for r in results if r.get("ok") and r["party_health"]["red_flag_total"] > 0]
    print(f"\nDONE. {len(ok)}/{len(results)} returned without error. "
          f"{len(flagged)} counties have party red-flags. Wrote {_OUT}", flush=True)
    for r in flagged:
        print(f"  ⚠ {r['county']}: {r['party_health']['red_flags']}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--conc", type=int, default=3)
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    only = [c.strip() for c in args.only.split(",") if c.strip()] or None
    asyncio.run(main(args.days, args.conc, only, args.timeout))
