"""Systematic county scraper test - 9 counties x all record types.

OLD = is_duplicate:true  (record existed in DB from a prior run)
NEW = is_duplicate:false (freshly scraped this run)

Usage:
  python scripts/test_counties_systematic.py
  python scripts/test_counties_systematic.py --county kitsap
  python scripts/test_counties_systematic.py --county kitsap --record-type probate
  python scripts/test_counties_systematic.py --dry-run
"""

import argparse
import sys
import time
from datetime import datetime

import requests
from _creds import admin_creds

API = "https://api.bridgeleads.io"
EMAIL, PASSWORD = admin_creds()

# Fixed 30-day window for reproducible results
DATE_FROM = "2026-04-01"
DATE_TO   = "2026-04-30"

# Ordered test matrix: county, state, record_type, template
TEST_MATRIX = [
    ("kitsap",   "wa", "probate",         "EagleWeb"),
    ("kitsap",   "wa", "pre_foreclosure", "EagleWeb"),
    ("lewis",    "wa", "probate",         "EagleWeb"),
    ("lewis",    "wa", "pre_foreclosure", "EagleWeb"),
    ("okanogan", "wa", "probate",         "TylerSelfService"),
    ("okanogan", "wa", "pre_foreclosure", "TylerSelfService"),
    ("pacific",  "wa", "probate",         "EagleWeb"),
    ("pacific",  "wa", "pre_foreclosure", "EagleWeb"),
    ("skagit",   "wa", "probate",         "SkagitRecording"),
    ("skagit",   "wa", "pre_foreclosure", "SkagitRecording"),
    ("skagit",   "wa", "tax_delinquent",  "SkagitRecording"),
    ("skagit",   "wa", "divorce",         "SkagitRecording"),
    ("spokane",  "wa", "probate",         "EagleWeb"),
    ("spokane",  "wa", "pre_foreclosure", "EagleWeb"),
    ("thurston", "wa", "probate",         "EagleWeb"),
    ("thurston", "wa", "pre_foreclosure", "EagleWeb"),
    ("whatcom",  "wa", "probate",         "Manual"),
    ("whatcom",  "wa", "pre_foreclosure", "Manual"),
    ("whitman",  "wa", "probate",         "EagleWeb"),
    ("whitman",  "wa", "pre_foreclosure", "EagleWeb"),
]

JOB_POLL_INTERVAL = 10   # seconds between polls
JOB_TIMEOUT       = 900  # 15 minutes max per job

SEP  = "=" * 60
SEP2 = "=" * 70
LINE = "-" * 70


def _login() -> dict:
    r = requests.post(f"{API}/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    if r.status_code != 200:
        sys.exit(f"[FATAL] Login failed {r.status_code}: {r.text[:200]}")
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _refresh(hdrs: dict) -> None:
    """Re-login and update hdrs in place when the JWT expires."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] JWT expired — re-logging in...")
    new = _login()
    hdrs.update(new)


def _api_get(hdrs: dict, url: str, **kwargs) -> requests.Response:
    r = requests.get(url, headers=hdrs, **kwargs)
    if r.status_code == 401:
        _refresh(hdrs)
        r = requests.get(url, headers=hdrs, **kwargs)
    r.raise_for_status()
    return r


def _api_post(hdrs: dict, url: str, **kwargs) -> requests.Response:
    r = requests.post(url, headers=hdrs, **kwargs)
    if r.status_code == 401:
        _refresh(hdrs)
        r = requests.post(url, headers=hdrs, **kwargs)
    r.raise_for_status()
    return r


def _get_or_create_config(hdrs: dict, county: str, state: str, record_type: str) -> str:
    """Return config_id for this county/state/record_type, creating if absent."""
    r = _api_get(hdrs, f"{API}/scrapers", timeout=10)
    existing = [
        c for c in r.json()
        if c["county"] == county
        and c["state"].lower() == state.lower()
        and c["record_type"] == record_type
    ]
    if existing:
        return existing[0]["id"]

    payload = {
        "name": f"{county.title()} {record_type.replace('_', ' ').title()} Systematic",
        "county": county,
        "state": state,
        "record_type": record_type,
        "fields": {
            "party_name": True, "parcel_id": True, "property_address": True,
            "mailing_address": True, "heirs": True, "legal_description": True,
            "date_recorded": True,
        },
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {
            "frequency": "manual",
            "date_range_mode": "custom",
            "date_from": DATE_FROM,
            "date_to":   DATE_TO,
        },
        "deliver": {"emails": [], "formats": ["csv"]},
    }
    r = _api_post(hdrs, f"{API}/scrapers", json=payload, timeout=10)
    return r.json()["id"]


def _trigger_job(hdrs: dict, config_id: str) -> str:
    r = _api_post(hdrs, f"{API}/jobs",
                  json={"scraper_config_id": config_id, "trigger": "manual"},
                  timeout=10)
    return r.json()["id"]


def _poll_job(hdrs: dict, job_id: str) -> dict:
    """Poll until terminal status, return the final job dict."""
    deadline = time.time() + JOB_TIMEOUT
    last_line = ""
    while time.time() < deadline:
        r = _api_get(hdrs, f"{API}/jobs/{job_id}", timeout=10)
        job = r.json()
        status   = job.get("status", "")
        progress = job.get("progress_label", status)
        rcount   = job.get("record_count", 0)

        line = f"{progress} | {rcount} records"
        if line != last_line:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"  [{ts}] {line}")
            last_line = line

        if status in ("done", "failed", "cancelled"):
            return job

        time.sleep(JOB_POLL_INTERVAL)

    return {"status": "timeout", "record_count": 0}


def _fetch_results(hdrs: dict, job_id: str, page_size: int = 500) -> list:
    """Fetch all results for a job (up to page_size)."""
    r = _api_get(
        hdrs,
        f"{API}/jobs/{job_id}/results",
        params={"page": 1, "page_size": page_size},
        timeout=15,
    )
    return r.json().get("items", [])


def _print_sample(records: list, n: int = 3) -> None:
    for i, rec in enumerate(records[:n], 1):
        name   = rec.get("party_name") or "n/a"
        date   = rec.get("date_recorded") or "n/a"
        parcel = rec.get("parcel_id") or "n/a"
        addr   = rec.get("property_address") or "n/a"
        print(f"    [{i}] {name} | {date} | parcel={parcel} | {addr}")


def run_test(county, state, record_type, template, hdrs, dry_run) -> dict:
    """Run one (county, record_type) test. Returns summary dict."""
    label = f"{county}/{record_type}"
    print(f"\n{SEP}")
    print(f"  {label.upper()}  [{template}]  {DATE_FROM} to {DATE_TO}")
    print(SEP)

    result = dict(county=county, record_type=record_type, template=template,
                  status="skip", new=0, old=0, total=0, job_status="", error="")

    if dry_run:
        print("  [DRY RUN] skipped")
        return result

    try:
        print("  [1/4] Locating scraper config...")
        config_id = _get_or_create_config(hdrs, county, state, record_type)
        print(f"        config_id={config_id}")

        print("  [2/4] Triggering job...")
        job_id = _trigger_job(hdrs, config_id)
        print(f"        job_id={job_id}")

        print(f"  [3/4] Polling (max {JOB_TIMEOUT}s)...")
        job = _poll_job(hdrs, job_id)
        result["job_status"] = job.get("status", "unknown")
        print(f"        Done: status={result['job_status']} records={job.get('record_count', 0)}")

        if result["job_status"] != "done":
            result["status"] = "FAIL"
            result["error"]  = f"job ended with status={result['job_status']}"
            print(f"  [FAIL] {result['error']}")
            return result

        print("  [4/4] Fetching results...")
        items       = _fetch_results(hdrs, job_id)
        new_records = [r for r in items if not r.get("is_duplicate", False)]
        old_records = [r for r in items if     r.get("is_duplicate", False)]

        result.update(new=len(new_records), old=len(old_records),
                      total=len(items), status="PASS")

        print(f"\n  RESULTS  NEW={result['new']}  OLD={result['old']}  TOTAL={result['total']}")

        if new_records:
            print("  -- NEW records (first 3):")
            _print_sample(new_records)
        else:
            print("  -- No NEW records (all duplicates or zero results)")

        if old_records:
            print(f"  -- OLD records ({len(old_records)} total, first 3):")
            _print_sample(old_records)

    except Exception as exc:
        result["status"] = "ERROR"
        result["error"]  = str(exc)[:200]
        print(f"  [ERROR] {result['error']}")

    return result


def print_summary(results: list) -> None:
    print(f"\n\n{SEP2}")
    print("  FINAL SUMMARY")
    print(SEP2)
    header = f"  {'COUNTY':<12} {'TYPE':<18} {'TEMPLATE':<18} {'STATUS':<6} {'NEW':>5} {'OLD':>5} {'TOTAL':>6}"
    print(header)
    print(f"  {LINE}")

    passes = fails = errors = 0
    for r in results:
        if r["status"] == "PASS":
            icon = "OK"
            passes += 1
        elif r["status"] == "FAIL":
            icon = "FAIL"
            fails += 1
        else:
            icon = "ERR"
            errors += 1
        print(f"  {icon:<4} {r['county']:<11} {r['record_type']:<18} {r['template']:<18} "
              f"{r['status']:<6} {r['new']:>5} {r['old']:>5} {r['total']:>6}")

    print(f"  {LINE}")
    print(f"  PASS={passes}  FAIL={fails}  ERROR/SKIP={errors}  TOTAL={len(results)}")

    failing = [r for r in results if r["status"] in ("FAIL", "ERROR")]
    if failing:
        print("\n  FAILURES:")
        for r in failing:
            print(f"    {r['county']}/{r['record_type']}: {r['error'] or r['job_status']}")
    print(f"{SEP2}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--county",      help="Filter to one county")
    parser.add_argument("--record-type", help="Filter to one record type")
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    matrix = TEST_MATRIX
    if args.county:
        matrix = [t for t in matrix if t[0] == args.county.lower()]
    if args.record_type:
        matrix = [t for t in matrix if t[2] == args.record_type.lower()]
    if not matrix:
        sys.exit("No matching combinations found.")

    print(f"\nBridgeLeads Systematic County Test")
    print(f"Date range : {DATE_FROM} to {DATE_TO}")
    print(f"Combos     : {len(matrix)}")
    print(f"Job timeout: {JOB_TIMEOUT}s")

    hdrs = {}
    if not args.dry_run:
        print("\nLogging in...")
        hdrs = _login()
        print("Logged in.\n")

    all_results = []
    for county, state, record_type, template in matrix:
        r = run_test(county, state, record_type, template, hdrs, args.dry_run)
        all_results.append(r)

    print_summary(all_results)


if __name__ == "__main__":
    main()
