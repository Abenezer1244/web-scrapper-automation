"""SAFE non-persisting live pre_foreclosure verification (current code).

Calls each county's pre_foreclosure scraper directly via the registry — NO Celery,
NO DB writes, NO enrichment, NO billing. Shows what CURRENT code produces for
party_name / heirs so we can confirm county-by-county whether the homeowner (not a
trustee/lender/law-firm) lands in party_name.

party_name classification uses the existing is_person_name heuristic as ONE signal
(not the source of truth) — eyeball the samples.

Usage (prod env for CAPTCHA_API_KEY etc.):
    railway run --service worker python scripts/live_verify_preforeclosure.py --only king --days 120
"""
import argparse
import asyncio
import inspect
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from src.scrapers.preforeclosure import is_person_name
from src.scrapers.registry import get_scraper_class

_STATE = "wa"
_RECORD_TYPE = "pre_foreclosure"
_OUT = Path("scripts/audit_out/preforeclosure_live.json")


def _classify(records):
    person = [r for r in records if (r.party_name or "").strip() and is_person_name(r.party_name)]
    company = [r for r in records if (r.party_name or "").strip() and not is_person_name(r.party_name)]
    empty = [r for r in records if not (r.party_name or "").strip()]
    return person, company, empty


def _summarize(county: str, records) -> dict:
    person, company, empty = _classify(records)
    n = len(records)
    samples = []
    for r in records[:25]:
        samples.append({
            "doc_type": r.doc_type,
            "party_name": (r.party_name or "")[:80],
            "heirs": (r.heirs or "")[:80],
            "parcel_id": r.parcel_id,
        })
    return {
        "county": county,
        "ok": True,
        "count": n,
        "person": len(person),
        "company": len(company),
        "empty": len(empty),
        "doc_types": dict(Counter((r.doc_type or "<none>") for r in records)),
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
            print(f"[{county}] OK count={res['count']} person={res['person']} "
                  f"company={res['company']} empty={res['empty']}", flush=True)
            for s in res["samples"]:
                print(f"    party={s['party_name']!r}  heirs={s['heirs']!r}", flush=True)
            return res
        except TimeoutError:
            print(f"[{county}] TIMEOUT after {timeout}s", flush=True)
            return {"county": county, "ok": False, "error": f"timeout_{timeout}s"}
        except Exception as exc:
            print(f"[{county}] SCRAPE ERROR: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            return {"county": county, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def main(days: int, conc: int, only: list[str], timeout: int) -> None:
    date_to = datetime.now()
    date_from = date_to - timedelta(days=days)
    df, dt = date_from.strftime("%m/%d/%Y"), date_to.strftime("%m/%d/%Y")
    counties = sorted(set(only))
    print(f"pre_foreclosure live verify: {counties}, window {df} -> {dt}, conc={conc}", flush=True)
    sem = asyncio.Semaphore(conc)
    results = await asyncio.gather(*[run_county(c, df, dt, sem, timeout) for c in counties])
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps({"window": [df, dt], "results": results}, indent=2))
    print(f"\nWrote {_OUT}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--conc", type=int, default=2)
    ap.add_argument("--only", type=str, required=True)
    ap.add_argument("--timeout", type=int, default=540)
    args = ap.parse_args()
    only = [c.strip() for c in args.only.split(",") if c.strip()]
    asyncio.run(main(args.days, args.conc, only, args.timeout))
