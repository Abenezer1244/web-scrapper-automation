"""READ-ONLY: measure the WA statewide GIS sweep for King parcels (Phase 4 diag).

Pulls real King parcel_ids from a job's results and times one statewide ArcGIS
chunk query (the path King falls through to, since King has no _KNOWN_GIS_ENDPOINTS
entry). Reports latency + hit count so we can see whether the sweep is slow,
empty, or both.

    railway run --service worker python scripts/diag_king_gis_latency.py <job_id>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.enrichment.county_gis import _WA_STATEWIDE_ENDPOINT
from src.utils.safe_http import safe_get


def main() -> None:
    job_id = sys.argv[1]
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT parcel_id FROM results WHERE job_id = :j "
                "AND parcel_id IS NOT NULL LIMIT 50"
            ),
            {"j": job_id},
        ).fetchall()
    pids = [r.parcel_id.replace("-", "").strip() for r in rows if r.parcel_id]
    print(f"sampled {len(pids)} King parcels; e.g. {pids[:3]}")
    if not pids:
        print("no parcels")
        return

    in_clause = ",".join(f"'{p}'" for p in pids)
    params = {
        "where": f"ORIG_PARCEL_ID IN ({in_clause}) AND FIPS_NR='033'",
        "outFields": "ORIG_PARCEL_ID,SITUS_ADDRESS,SITUS_CITY_NM,SITUS_ZIP_NR",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": 50,
    }
    t0 = time.monotonic()
    try:
        resp = safe_get(_WA_STATEWIDE_ENDPOINT, params=params, timeout=30)
        elapsed = time.monotonic() - t0
        feats = (resp.json().get("features") or []) if resp.status_code == 200 else []
        print(f"status={resp.status_code} elapsed={elapsed:.2f}s "
              f"hits={len(feats)}/{len(pids)}")
        # Extrapolate the full-sweep cost at this per-chunk latency.
        chunks = (24708 + 49) // 50
        print(f"~{chunks} chunks for 24,708 parcels → est sweep "
              f"~{chunks * elapsed / 60:.1f} min at this latency")
    except Exception as exc:
        print(f"ERROR after {time.monotonic() - t0:.2f}s: {type(exc).__name__}: {str(exc)[:200]}")


if __name__ == "__main__":
    main()
