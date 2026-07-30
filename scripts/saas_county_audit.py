"""Live SaaS county audit — drive the real BridgeLeads platform, one county at a time.

This runs the REAL scraping engine on the BridgeLeads SaaS (Railway, headed
Chromium via Xvfb). It triggers a real job per (county, record_type) over a
3-month window, polls each to completion (scrape -> enrich -> export), and
captures status + record count + field coverage + error for every job.

Mirrors the proven login/poll logic of scripts/test_counties_systematic.py,
generalized to ALL active connectors and a custom 3-month window, writing an
incremental report so progress survives interruption.

Usage:
    python scripts/saas_county_audit.py                 # all combos, sequential
    python scripts/saas_county_audit.py --county king   # one county, all its types
    python scripts/saas_county_audit.py --county pierce --record-type probate
    python scripts/saas_county_audit.py --only-failed   # re-run combos that previously failed
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from _creds import admin_creds

API = "https://api.bridgeleads.io"
EMAIL, PASSWORD = admin_creds()

# 3-month window (ISO for the config payload; the worker converts to %m/%d/%Y).
DATE_FROM = "2026-03-03"
DATE_TO = "2026-06-03"

JOB_POLL_INTERVAL = 10
JOB_TIMEOUT = 900  # 15 min per job

OUT_DIR = Path(__file__).resolve().parent / "audit_out_saas"
FIELDS = ("party_name", "parcel_id", "property_address", "mailing_address", "date_recorded", "heirs")


# ─── Auth + API helpers (re-login on 401) ──────────────────────────────────────

def _login() -> dict:
    r = requests.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    if r.status_code != 200:
        sys.exit(f"[FATAL] Login failed {r.status_code}: {r.text[:200]}")
    return {"Authorization": f"Bearer {r.json()['access_token']}", "Content-Type": "application/json"}


def _refresh(hdrs: dict) -> None:
    print(f"  [{datetime.now():%H:%M:%S}] JWT expired — re-logging in...")
    hdrs.update(_login())


# Every call goes through these helpers, so default the timeout here rather than
# at ~20 call sites. Without it a hung API leaves the audit blocked forever with
# no output. An explicit timeout= from a caller still wins.
_TIMEOUT = 30


def _get(hdrs, url, **kw):
    kw.setdefault("timeout", _TIMEOUT)
    r = requests.get(url, headers=hdrs, **kw)          # noqa: S113 — timeout set above
    if r.status_code == 401:
        _refresh(hdrs)
        r = requests.get(url, headers=hdrs, **kw)      # noqa: S113 — timeout set above
    return r


def _post(hdrs, url, **kw):
    kw.setdefault("timeout", _TIMEOUT)
    r = requests.post(url, headers=hdrs, **kw)         # noqa: S113 — timeout set above
    if r.status_code == 401:
        _refresh(hdrs)
        r = requests.post(url, headers=hdrs, **kw)     # noqa: S113 — timeout set above
    return r


# ─── Matrix ────────────────────────────────────────────────────────────────────

def get_matrix(hdrs, county_filter=None, rt_filter=None) -> list[tuple[str, str, str]]:
    r = _get(hdrs, f"{API}/scrapers/connectors", timeout=20)
    r.raise_for_status()
    combos, seen = [], set()
    for c in sorted(r.json(), key=lambda x: (x["state"], x["county"])):
        if not c.get("active"):
            continue
        county, state = c["county"].lower(), c["state"].lower()
        if county_filter and county != county_filter.lower():
            continue
        for rt in (c.get("record_types") or []):
            rt = rt.lower()
            if rt_filter and rt != rt_filter.lower():
                continue
            key = (county, state, rt)
            if key not in seen:
                seen.add(key)
                combos.append(key)
    return combos


def get_or_create_config(hdrs, county, state, record_type) -> str:
    """Find/create an AUDIT config with the exact 3-month custom window."""
    name = f"AUDIT {county} {record_type} {DATE_TO}"
    r = _get(hdrs, f"{API}/scrapers", timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c["id"]

    payload = {
        "name": name,
        "county": county, "state": state, "record_type": record_type,
        "fields": dict.fromkeys(("party_name", "parcel_id", "property_address", "mailing_address", "heirs", "legal_description", "date_recorded"), True),
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "custom",
                     "date_from": DATE_FROM, "date_to": DATE_TO},
        "deliver": {"emails": [], "formats": ["csv"]},
    }
    r = _post(hdrs, f"{API}/scrapers", json=payload, timeout=15)
    if r.status_code != 201:
        raise RuntimeError(f"config create failed {r.status_code}: {r.text[:200]}")
    return r.json()["id"]


def trigger_job(hdrs, config_id) -> str:
    r = _post(hdrs, f"{API}/jobs", json={"scraper_config_id": config_id, "trigger": "manual"}, timeout=15)
    if r.status_code != 201:
        raise RuntimeError(f"trigger failed {r.status_code}: {r.text[:200]}")
    return r.json()["id"]


def poll_job(hdrs, job_id) -> dict:
    deadline = time.time() + JOB_TIMEOUT
    last = ""
    while time.time() < deadline:
        r = _get(hdrs, f"{API}/jobs/{job_id}", timeout=15)
        if r.status_code != 200:
            time.sleep(JOB_POLL_INTERVAL)
            continue
        job = r.json()
        status = job.get("status", "")
        label = job.get("progress_label", status)
        rc = job.get("record_count", 0)
        line = f"{label} | {rc} rec"
        if line != last:
            print(f"    [{datetime.now():%H:%M:%S}] {line}")
            last = line
        if status in ("done", "failed", "cancelled"):
            return job
        time.sleep(JOB_POLL_INTERVAL)
    return {"status": "timeout", "record_count": 0, "id": job_id}


def fetch_results(hdrs, job_id, page_size=200) -> list:
    r = _get(hdrs, f"{API}/jobs/{job_id}/results",
             params={"page": 1, "page_size": page_size}, timeout=20)
    if r.status_code != 200:
        return []
    return r.json().get("items", [])


def coverage(items: list) -> dict:
    n = len(items)
    return {f: {"count": sum(1 for r in items if r.get(f)),
                "pct": (sum(1 for r in items if r.get(f)) * 100 // n) if n else 0}
            for f in FIELDS}


# ─── Report ────────────────────────────────────────────────────────────────────

def write_report(results: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    def cov(r, f):
        return r.get("coverage", {}).get(f, {}).get("pct", "")

    lines = [
        "# Live SaaS County Audit (real jobs on api.bridgeleads.io)",
        "",
        f"Window: **{DATE_FROM} -> {DATE_TO}** | Updated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "| County | Type | Status | Job | Records | name% | parcel% | propAddr% | mailAddr% | date% | Dur(s) | Error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    npass = nempty = nfail = 0
    for r in results:
        st = r["status"]
        if st == "done" and r.get("record_count", 0) > 0:
            verdict = "PASS"
            npass += 1
        elif st == "done":
            verdict = "EMPTY"
            nempty += 1
        else:
            verdict = st.upper()
            nfail += 1
        lines.append(
            f"| {r['county']} | {r['record_type']} | {verdict} | {str(r.get('job_id',''))[:8]} | "
            f"{r.get('record_count',0)} | {cov(r,'party_name')} | {cov(r,'parcel_id')} | "
            f"{cov(r,'property_address')} | {cov(r,'mailing_address')} | {cov(r,'date_recorded')} | "
            f"{r.get('duration_s','')} | {(r.get('error') or '')[:60]} |"
        )
    lines += ["", f"**PASS={npass}  EMPTY={nempty}  FAIL={nfail}  TOTAL={len(results)}**"]
    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


# ─── Run one combo ─────────────────────────────────────────────────────────────

def run_combo(hdrs, county, state, record_type) -> dict:
    print(f"\n{'='*60}\n  {county.upper()} / {record_type}  ({DATE_FROM} -> {DATE_TO})\n{'='*60}")
    res = {"county": county, "state": state, "record_type": record_type,
           "status": "error", "record_count": 0, "coverage": {}, "samples": [],
           "job_id": None, "error": None, "duration_s": 0,
           "ran_at": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    try:
        cfg = get_or_create_config(hdrs, county, state, record_type)
        print(f"  config={cfg}")
        job_id = trigger_job(hdrs, cfg)
        res["job_id"] = job_id
        print(f"  job={job_id}  polling (max {JOB_TIMEOUT}s)...")
        job = poll_job(hdrs, job_id)
        res["status"] = job.get("status", "unknown")
        res["record_count"] = job.get("record_count", 0)
        res["error"] = job.get("error_message")
        if res["status"] == "done" and res["record_count"] > 0:
            items = fetch_results(hdrs, job_id)
            res["coverage"] = coverage(items)
            res["samples"] = items[:3]
    except Exception as exc:
        res["status"] = "error"
        res["error"] = repr(exc)[:300]
    res["duration_s"] = round(time.time() - t0, 1)
    icon = "OK" if (res["status"] == "done" and res["record_count"] > 0) else (
        "EMPTY" if res["status"] == "done" else "FAIL")
    print(f"  [{icon}] {res['status']} — {res['record_count']} records ({res['duration_s']}s)")
    if res["error"]:
        print(f"  error: {res['error'][:200]}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county")
    ap.add_argument("--record-type")
    ap.add_argument("--only-failed", action="store_true",
                    help="Only re-run combos that failed in the last summary.json")
    args = ap.parse_args()

    print("Logging in to live SaaS...")
    hdrs = _login()

    matrix = get_matrix(hdrs, args.county, args.record_type)

    if args.only_failed:
        prev_path = OUT_DIR / "summary.json"
        if prev_path.exists():
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
            failed = {(p["county"], p["record_type"]) for p in prev
                      if not (p["status"] == "done" and p.get("record_count", 0) > 0)}
            matrix = [m for m in matrix if (m[0], m[2]) in failed]

    if not matrix:
        sys.exit("No matching combos.")

    print(f"\n{'#'*60}\n# LIVE SAAS AUDIT — {len(matrix)} combos — {DATE_FROM} -> {DATE_TO}\n{'#'*60}")

    # Preserve prior results for combos we're not re-running (so report stays complete).
    results = []
    prev_path = OUT_DIR / "summary.json"
    if args.only_failed and prev_path.exists():
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        rerun = {(m[0], m[2]) for m in matrix}
        results = [p for p in prev if (p["county"], p["record_type"]) not in rerun]

    for i, (county, state, rt) in enumerate(matrix, 1):
        print(f"\n[{i}/{len(matrix)}]", end="")
        results.append(run_combo(hdrs, county, state, rt))
        write_report(results)

    print(f"\nDONE. Report: {OUT_DIR / 'summary.md'}")


if __name__ == "__main__":
    main()
