"""Canary: does phase-1-first actually reach the parcels we claim, at scale?

WHAT IS UNVERIFIED
    #215 split the King enrichment so phase 1 (one cheap HTTP GET per parcel ->
    property + OWNER) runs across EVERY parcel before phase 2 (Playwright mailing,
    10-20x costlier) gets any budget. Before that split, chunking made chunk 1's
    phase 2 eat the whole budget: a real 17,157-parcel job reached 173 parcels.
    The fix predicts ~1,200 in the same 600s. That number has never been observed:
    three live app runs were killed mid-job by unrelated deploys, and the larger
    A/B tripped King's rate limit.

WHY THIS SHAPE (Codex's "minimum sufficient verification")
    * ONE arm only. Re-running the old shape for comparison doubles traffic against
      a source that has already throttled us once - the prediction is derived from
      the code, so the canary only has to confirm the new path reaches its number.
    * PHASE 1 ONLY (do_mailing=False). Phase 2 is what made the old shape slow; it
      is not what we are measuring, and skipping it removes Playwright entirely.
    * PRODUCTION PACING. The throttle incident came from pace_s=0.05, 2-4x faster
      than production. This defaults to the production value and refuses to go
      faster - a canary that trips the source proves nothing and costs a day.
    * Standalone process, so a worker redeploy cannot kill it mid-measurement.

    It reads only. Nothing is written to any lead row.

USAGE (after the source-health cooldown has expired)
    railway run python scripts/canary_king_phase1_throughput.py --parcels 1500
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.enrichment.king_county_assessor import (  # noqa: E402
    batch_enrich_king_county,
)
from src.scrapers.enrichment.source_health import (  # noqa: E402
    KING_EREALPROPERTY,
    is_source_available,
)

_API = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
# Production phase-1 pacing. Going below this is what tripped King's throttle.
_MIN_PACE_S = 0.1
_CHUNK = 200
_BUDGET_S = 600.0


def _real_parcels(n: int) -> list[str]:
    """Real delinquent King parcels, so the canary measures real lookups."""
    out: list[str] = []
    seen: set[str] = set()
    off = 0
    while len(out) < n:
        r = requests.get(
            _API,
            params={"$select": "account_number", "$where": "bill_year='2026'",
                    "$limit": 2000, "$offset": off, "$order": ":id"},
            headers=_HEADERS, timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        for it in page:
            a = (it.get("account_number") or "").strip()
            if len(a) == 12 and a.isdigit() and a[:10] not in seen:
                seen.add(a[:10])
                out.append(a[:10])
                if len(out) >= n:
                    break
        off += 2000
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parcels", type=int, default=1500)
    ap.add_argument("--budget", type=float, default=_BUDGET_S)
    ap.add_argument("--pace", type=float, default=_MIN_PACE_S)
    args = ap.parse_args()

    if args.pace < _MIN_PACE_S:
        raise SystemExit(
            f"refusing pace_s={args.pace}: below the production floor {_MIN_PACE_S}. "
            "An over-aggressive probe is what throttled eRealProperty for 24h."
        )

    with system_sync_session() as db:
        if not is_source_available(db, KING_EREALPROPERTY):
            raise SystemExit(
                "refusing: eRealProperty is still marked unavailable (cooldown). "
                "Wait for it to expire - running now would re-trip the breaker."
            )
    print(f"source-health gate: OK\nfetching {args.parcels} real King parcels...")
    parcels = _real_parcels(args.parcels)
    print(f"  got {len(parcels)}")

    deadline = time.monotonic() + args.budget
    pending = list(parcels)
    tax_urls: dict[str, str] = {}
    reached = owners = props = 0
    chunks = 0
    t0 = time.monotonic()

    while pending:
        left = deadline - time.monotonic()
        if left <= 5:
            print(f"  budget spent; {len(pending)} parcels not reached")
            break
        chunk, pending = pending[:_CHUNK], pending[_CHUNK:]
        stats: dict = {}
        try:
            res = asyncio.run(batch_enrich_king_county(
                chunk, time_budget_s=left, stats=stats, pace_s=args.pace,
                do_mailing=False, tax_urls_out=tax_urls,
            ))
        except Exception as exc:  # noqa: BLE001 - a tripped breaker is a real result
            print(f"  ABORTED on chunk {chunks + 1}: {type(exc).__name__}: {str(exc)[:160]}")
            break
        chunks += 1
        reached += len(res)
        owners += sum(1 for d in res.values() if d.get("owner_name"))
        props += sum(1 for d in res.values() if d.get("property_address"))
        print(f"  chunk {chunks}: reached={reached} owners={owners} "
              f"elapsed={time.monotonic() - t0:.0f}s")
        if stats.get("budget_exhausted"):
            print(f"  budget exhausted inside chunk {chunks}; "
                  f"{len(pending)} parcels not reached")
            break

    elapsed = time.monotonic() - t0
    print("\n=== RESULT ===")
    print(f"  budget                 : {args.budget:.0f}s   elapsed: {elapsed:.0f}s")
    print(f"  chunks committed       : {chunks} (chunk size {_CHUNK})")
    print(f"  parcels reached        : {reached} of {len(parcels)}")
    print(f"  owner names            : {owners}")
    print(f"  property addresses     : {props}")
    print(f"  tax URLs for later mail: {len(tax_urls)}")
    if reached:
        print(f"  per-parcel latency     : {elapsed / reached:.3f}s")
    with system_sync_session() as db:
        still_ok = is_source_available(db, KING_EREALPROPERTY)
    print(f"  source still healthy   : {still_ok}")

    # The pre-fix shape was structurally capped at ONE chunk of phase 1 before
    # phase 2 drained the rest, so crossing a chunk boundary is the fix working.
    print("\n=== VERDICT ===")
    if not still_ok:
        print("  FAIL - the source breaker tripped during the canary.")
    elif chunks >= 2 and reached > _CHUNK:
        print(f"  PASS - phase-1-only reached {reached} parcels across {chunks} chunks; "
              f"the old shape could not exceed {_CHUNK} (it spent the rest on mailing).")
    else:
        print(f"  INCONCLUSIVE - only {reached} parcels in {chunks} chunk(s). "
              "Expected >200 across 2+ chunks; check latency and non-200 rate above.")

    # Scope, stated rather than implied (Codex P1/P2). This script drives
    # batch_enrich_king_county directly, so a PASS is evidence about the HELPER's
    # phase-1-only contract and King's real latency — NOT proof that the deployed
    # worker orchestrates the two passes correctly. That caller lives in
    # src/workers/tasks_helpers/enrich.py and is pinned by
    # tests/test_king_two_pass_contract.py; a green canary plus that test is what
    # covers the pair. Only a real job exercises phase 2, the deferred-marker
    # volume and the per-chunk commits at 17k scale.
    print("\n  PROVES     : phase-1-only crosses chunk boundaries; live per-parcel cost.")
    print("  DOES NOT   : exercise the deployed worker's two-pass caller, phase 2,")
    print("               per-chunk commits, or the 17k-parcel budget-expiry path.")
    print(f"  NOTE       : latency above is seconds per USEFUL RESULT ({reached}), not")
    print("               per attempted request - misses and non-200s inflate it.")


if __name__ == "__main__":
    main()
