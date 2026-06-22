"""Live divorce probe — runs the LOCAL scraper code against the real county
portals for every ACTIVE divorce connector (Pierce, Skagit), mirroring the
worker's _run_scraper instantiation (record_type passed via signature inspect).

Proves the new divorce classifier on real data: prints record count, party-name
samples, and flags any record that looks like a corporate/entity dissolution
leaking into divorce results (the bug this campaign fixes).

Run:  railway run python scripts/diag_divorce_live.py [--days 90]
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

# Company/entity tokens that should NEVER appear as a divorce party_name — a hit
# here means a corporate dissolution leaked into divorce results.
_LEAK_TOKENS = (
    " LLC", " L.L.C", " INC", " CORP", " CORPORATION", " COMPANY",
    " PARTNERSHIP", " NONPROFIT", "ARTICLES OF DISSOLUTION",
    "CERTIFICATE OF DISSOLUTION", " LP ", " LLP",
)

DIVORCE_CONNECTORS = [("pierce", "wa"), ("skagit", "WA")]


async def probe(county: str, state: str, days: int) -> None:
    from src.scrapers.registry import get_scraper_class

    today = datetime.now(UTC).date()
    df = (today - timedelta(days=days)).strftime("%m/%d/%Y")
    dt = today.strftime("%m/%d/%Y")
    tag = f"{county}/{state}/divorce"

    try:
        cls, _ = get_scraper_class(county, state, "divorce")
    except Exception as exc:  # noqa: BLE001
        print(f"{tag}: REGISTRY FAIL {type(exc).__name__}: {str(exc)[:140]}")
        return

    kwargs = {}
    try:
        if "record_type" in inspect.signature(cls).parameters:
            kwargs["record_type"] = "divorce"
    except (ValueError, TypeError):
        pass

    print(f"\n=== {tag}  window {df}..{dt}  ({days}d) ===")
    try:
        async with cls(**kwargs) as s:
            records = await s.scrape(df, dt)
    except Exception as exc:  # noqa: BLE001
        print(f"{tag}: SCRAPE FAIL {type(exc).__name__}: {str(exc)[:200]}")
        return

    n = len(records)
    leaks = []
    for rec in records:
        pn = (rec.party_name or "").upper()
        dtp = (rec.doc_type or "").upper()
        blob = f"{pn} {dtp}"
        if any(tok in f" {blob} " for tok in _LEAK_TOKENS):
            leaks.append(rec)

    print(f"{tag}: count={n}  corporate_leaks={len(leaks)}")
    print("  sample party_name | heirs | doc_type:")
    for rec in records[:10]:
        print(f"    - {(rec.party_name or '')[:48]:<48} | {(rec.heirs or '')[:24]:<24} | {rec.doc_type or ''}")
    if leaks:
        print("  !!! POTENTIAL CORPORATE-DISSOLUTION LEAKS:")
        for rec in leaks[:10]:
            print(f"    *** {rec.party_name!r}  doc_type={rec.doc_type!r}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    from src.api.middleware.security import register_connector_domains_from_db
    n = register_connector_domains_from_db()
    print(f"registered {n} connector domains for SSRF allowlist")

    for county, state in DIVORCE_CONNECTORS:
        await probe(county, state, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
