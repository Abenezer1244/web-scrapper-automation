"""End-to-end live test via production API — triggers a real Pierce County
scrape, shows real-time progress with estimated record count, validates
results, and downloads the CSV export.

Usage:
    python scripts/e2e_live_chromium.py
    python scripts/e2e_live_chromium.py --county pierce   # default
    python scripts/e2e_live_chromium.py --skip-register    # reuse saved creds
"""

import json
import os
import sys
import time
import uuid

import requests
from _creds import test_password

API = "https://api.bridgeleads.io"
POLL_INTERVAL = 10
MAX_WAIT = 900  # 15 minutes
CREDS_FILE = "scripts/.e2e_pierce_live.json"


def api(method, path, token=None, data=None, stream=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(
        method, f"{API}{path}", headers=headers, json=data,
        timeout=30, stream=stream,
    )


def register():
    email = f"e2e_live_{uuid.uuid4().hex[:8]}@bridgeleads.io"
    password = test_password()
    print(f"  Email: {email}")

    r = api("POST", "/auth/register", data={"email": email, "password": password})
    if r.status_code != 201:
        print(f"  FAIL: {r.status_code} {r.text[:200]}")
        sys.exit(1)

    token = r.json()["access_token"]
    me = api("GET", "/auth/me", token=token)
    user_id = me.json()["id"]
    print(f"  User ID: {user_id}")
    return token, user_id, email


def create_config(token, county="pierce", state="wa"):
    r = api("POST", "/scrapers", token=token, data={
        "name": f"E2E {county.title()} {state.upper()} Probate Live",
        "county": county,
        "state": state,
        "record_type": "probate",
        "fields": {
            "party_name": True, "parcel_id": True,
            "property_address": True, "mailing_address": True,
            "heirs": True, "legal_description": True, "date_recorded": True,
        },
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "rolling_90"},
        "deliver": {"emails": [], "formats": ["csv"]},
    })
    if r.status_code != 201:
        print(f"  FAIL: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    return r.json()["id"]


def trigger_job(token, config_id):
    r = api("POST", "/jobs", token=token, data={
        "scraper_config_id": config_id,
        "trigger": "manual",
    })
    if r.status_code != 201:
        print(f"  FAIL: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    return r.json()["id"]


def format_time(secs):
    """Format seconds as human-readable string."""
    if secs is None:
        return "?"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    secs = secs % 60
    return f"{mins}m {secs}s"


def poll_with_progress(token, job_id):
    """Poll job status with real-time progress bar, ETA, and record estimates."""
    start = time.time()
    last_status = None

    while True:
        elapsed = int(time.time() - start)
        if elapsed > MAX_WAIT:
            print(f"\n  TIMEOUT after {elapsed}s")
            return None

        r = api("GET", f"/jobs/{job_id}", token=token)
        if r.status_code != 200:
            print(f"\r  [{elapsed:>4}s] Poll error: {r.status_code}    ", end="", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        job = r.json()
        status = job["status"]
        rc = job.get("record_count", 0)
        page_cur = job.get("page_current", 0)
        page_tot = job.get("page_total", 0)

        # Use API-computed fields if available, fall back to local calc
        pct = job.get("progress_pct")
        est_total = job.get("estimated_total_records")
        eta_secs = job.get("estimated_seconds_remaining")
        elapsed_api = job.get("elapsed_seconds")

        # Build progress line
        if status == "scraping" and pct is not None and pct > 0:
            bar_len = 30
            filled = int(bar_len * pct / 100)
            bar = "#" * filled + "-" * (bar_len - filled)
            est_str = f"~{est_total}" if est_total else "?"
            eta_str = format_time(eta_secs) if eta_secs is not None else "?"
            line = (
                f"\r  [{elapsed:>4}s] {status:<10} "
                f"[{bar}] {pct:>3}%  "
                f"pg {page_cur}/{page_tot}  "
                f"rec: {rc} (est {est_str})  "
                f"ETA: {eta_str}"
            )
        elif status == "scraping":
            spinner = ["|", "/", "-", "\\"][elapsed % 4]
            line = f"\r  [{elapsed:>4}s] {status:<10} {spinner} records so far: {rc}"
        elif status == "enriching":
            line = f"\r  [{elapsed:>4}s] {status:<10} enriching {rc} records..."
        elif status in ("done", "failed", "cancelled"):
            duration = format_time(elapsed_api) if elapsed_api else format_time(elapsed)
            print(f"\r  [{elapsed:>4}s] {status:<10} -- {rc} records in {duration}" + " " * 30)
            return job
        else:
            line = f"\r  [{elapsed:>4}s] {status:<10}"

        print(line + " " * 5, end="", flush=True)

        # Print status transitions on new line
        if status != last_status:
            if last_status is not None:
                print()
            last_status = status

        time.sleep(POLL_INTERVAL)


def validate_results(token, job_id, total):
    """Fetch sample results and show field coverage."""
    r = api("GET", f"/jobs/{job_id}/results?page=1&page_size=50", token=token)
    if r.status_code != 200:
        print(f"  Results fetch failed: {r.status_code}")
        return

    data = r.json()
    items = data.get("items", [])
    n = len(items)

    has_name = sum(1 for i in items if i.get("party_name"))
    has_parcel = sum(1 for i in items if i.get("parcel_id"))
    has_prop = sum(1 for i in items if i.get("property_address"))
    has_mail = sum(1 for i in items if i.get("mailing_address"))
    has_date = sum(1 for i in items if i.get("date_recorded"))
    has_heirs = sum(1 for i in items if i.get("heirs"))

    def pct(x):
        return f"{x*100//n}%" if n else "0%"

    print(f"  Field coverage (sample of {n}):")
    print(f"    party_name:       {has_name}/{n} ({pct(has_name)})")
    print(f"    date_recorded:    {has_date}/{n} ({pct(has_date)})")
    print(f"    parcel_id:        {has_parcel}/{n} ({pct(has_parcel)})")
    print(f"    property_address: {has_prop}/{n} ({pct(has_prop)})")
    print(f"    mailing_address:  {has_mail}/{n} ({pct(has_mail)})")
    print(f"    heirs:            {has_heirs}/{n} ({pct(has_heirs)})")

    # Show 3 sample records
    print("\n  Sample records:")
    for i, rec in enumerate(items[:3]):
        print(f"    [{i+1}] {rec.get('party_name','?')} | "
              f"{rec.get('date_recorded','?')} | "
              f"PID={rec.get('parcel_id','?')} | "
              f"Addr={rec.get('property_address','?')}")

    return items


def download_csv(token, job_id):
    """Download the CSV export via presigned URL."""
    print("\n  Requesting download URL...")
    r = api("GET", f"/jobs/{job_id}/export-url", token=token)
    if r.status_code == 404:
        print("  No export available (job may not have generated CSV)")
        return None
    if r.status_code != 200:
        print(f"  Export URL failed: {r.status_code} {r.text[:100]}")
        return None

    url = r.json().get("url")
    if not url:
        print("  No URL in response")
        return None

    # Download the file
    print("  Downloading CSV...")
    dl = requests.get(url, timeout=60, stream=True)
    if dl.status_code != 200:
        print(f"  Download failed: {dl.status_code}")
        return None

    filename = f"e2e_pierce_export_{job_id[:8]}.csv"
    filepath = os.path.join("scripts", filename)
    total_bytes = 0
    with open(filepath, "wb") as f:
        for chunk in dl.iter_content(chunk_size=8192):
            f.write(chunk)
            total_bytes += len(chunk)

    # Count rows
    with open(filepath, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    row_count = len(lines) - 1  # minus header

    size_kb = total_bytes / 1024
    print(f"  Downloaded: {filepath}")
    print(f"  Size: {size_kb:.1f} KB ({row_count} data rows)")

    # Show header + first 3 rows
    if lines:
        print("\n  CSV preview:")
        print(f"    Header: {lines[0].strip()[:120]}")
        for i, line in enumerate(lines[1:4]):
            print(f"    [{i+1}] {line.strip()[:120]}")

    return filepath


def main():
    county = "pierce"
    skip_register = "--skip-register" in sys.argv

    if "--county" in sys.argv:
        idx = sys.argv.index("--county")
        if idx + 1 < len(sys.argv):
            county = sys.argv[idx + 1].lower()

    print("=" * 70)
    print("BridgeLeads E2E Live Test")
    print("=" * 70)
    print(f"  API:    {API}")
    print(f"  County: {county}")
    print()

    # Step 1: Auth
    if skip_register and os.path.exists(CREDS_FILE):
        print("[1/5] Reusing saved credentials...")
        creds = json.load(open(CREDS_FILE))
        token = creds["token"]
        # Verify token
        r = api("GET", "/auth/me", token=token)
        if r.status_code != 200:
            print("  Token expired, registering fresh...")
            token, user_id, email = register()
        else:
            email = creds.get("email", "?")
            print(f"  Email: {email}")
    else:
        print("[1/5] Registering test account...")
        token, user_id, email = register()

    # Step 2: Config
    print("\n[2/5] Creating scraper config...")
    config_id = create_config(token, county)
    print(f"  Config ID: {config_id}")

    # Step 3: Trigger
    print("\n[3/5] Triggering scrape job...")
    job_id = trigger_job(token, config_id)
    print(f"  Job ID: {job_id}")

    # Save state
    with open(CREDS_FILE, "w") as f:
        json.dump({
            "email": email, "token": token,
            "config_id": config_id, "job_id": job_id,
        }, f, indent=2)

    # Step 4: Poll with live progress
    print("\n[4/5] Scraping in progress...")
    job = poll_with_progress(token, job_id)

    if not job or job["status"] != "done":
        err = (job or {}).get("error_message", "unknown")
        print(f"\n  FAILED: {err}")
        sys.exit(1)

    rc = job.get("record_count", 0)
    started = job.get("started_at", "?")
    finished = job.get("finished_at", "?")
    print(f"  Duration: {started} -> {finished}")

    # Step 5: Validate + Download
    print(f"\n[5/5] Validating {rc} records...")
    validate_results(token, job_id, rc)

    filepath = download_csv(token, job_id)

    # Final summary
    print(f"\n{'=' * 70}")
    print("  PASSED")
    print(f"  County:  {county}")
    print(f"  Records: {rc}")
    if filepath:
        print(f"  CSV:     {filepath}")
    print(f"  Job ID:  {job_id}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
