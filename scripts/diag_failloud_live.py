"""Live fail-loud regression probe — runs the LOCAL hardened scrapers against the
real portals for one representative ACTIVE county per template, mirroring the
worker's _run_scraper instantiation.

Confirms the PR2 fail-loud changes do NOT false-raise on a normal window-with-data
(a raised ScraperExecutionError on a populated window = regression), and that a
genuine zero-result future window returns [] (no raise).

Run: railway run python scripts/diag_failloud_live.py [--days 21]
"""
import argparse
import asyncio
import inspect
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# One active county per PR2 template (ava_fidlar has no active connector -> skipped).
TARGETS = [
    ("clark", "WA", "probate", "landmarkweb"),
    ("chelan", "WA", "probate", "acclaimweb"),
    ("okanogan", "WA", "probate", "tyler_selfservice"),
    ("skagit", "WA", "probate", "skagit_recording"),
]


async def _run(county, state, rt, df, dt):
    from src.scrapers.registry import get_scraper_class
    cls, _ = get_scraper_class(county, state, rt)
    kwargs = {}
    try:
        if "record_type" in inspect.signature(cls).parameters:
            kwargs["record_type"] = rt
    except (ValueError, TypeError):
        pass
    async with cls(**kwargs) as s:
        return await s.scrape(df, dt)


async def probe(county, state, rt, template, days):
    from src.scrapers.reliability import ScraperExecutionError
    today = datetime.now(UTC).date()
    df = (today - timedelta(days=days)).strftime("%m/%d/%Y")
    dt = today.strftime("%m/%d/%Y")
    tag = f"{county}/{state}/{rt} [{template}]"

    # 1) Populated window — must NOT raise (records >= 0; a raise here = regression).
    try:
        recs = await _run(county, state, rt, df, dt)
        print(f"OK   {tag}: populated window {df}..{dt} -> {len(recs)} records (no false-raise)")
    except ScraperExecutionError as exc:
        print(f"FAIL {tag}: populated window RAISED ScraperExecutionError -> {str(exc)[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN {tag}: populated window raised {type(exc).__name__}: {str(exc)[:160]}")

    # 2) Genuine-empty future window — should return [] (genuine empty), not raise.
    fdf = (today + timedelta(days=400)).strftime("%m/%d/%Y")
    fdt = (today + timedelta(days=420)).strftime("%m/%d/%Y")
    try:
        recs = await _run(county, state, rt, fdf, fdt)
        print(f"OK   {tag}: future-empty window {fdf}..{fdt} -> {len(recs)} records (genuine empty handled)")
    except Exception as exc:  # noqa: BLE001
        print(f"NOTE {tag}: future-empty window raised {type(exc).__name__}: {str(exc)[:160]} "
              "(acceptable IF portal rejects future dates; review)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--only", default=None, help="county slug to probe alone")
    args = ap.parse_args()

    from src.api.middleware.security import register_connector_domains_from_db
    print(f"registered {register_connector_domains_from_db()} connector domains")
    for county, state, rt, template in TARGETS:
        if args.only and county != args.only:
            continue
        await probe(county, state, rt, template, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
