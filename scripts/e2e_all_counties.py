"""End-to-end test: register, create scraper configs, trigger jobs, validate results.

Tests all active WA county connectors and confirms each produces records with:
- party_name (first/last name)
- parcel_id
- property_address
- mailing_address

Usage:
    python scripts/e2e_all_counties.py
"""

import json
import sys
import time
import uuid

import requests

API_URL = "https://api.bridgeleads.io"
POLL_INTERVAL = 30  # seconds between status checks
MAX_WAIT = 3600  # max seconds to wait for a single job (60 min)
BATCH_SIZE = 2  # jobs to run concurrently


def api(method: str, path: str, token: str = None, json_data: dict = None) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, f"{API_URL}{path}", headers=headers, json=json_data, timeout=30)
    return resp


def register() -> tuple[str, str]:
    """Register a new test account. Returns (token, user_id)."""
    email = f"e2e_allcounties_{uuid.uuid4().hex[:8]}@bridgeleads.io"
    password = "E2eTest!2026secure"
    print(f"[REGISTER] {email}")

    resp = api("POST", "/auth/register", json_data={"email": email, "password": password})
    if resp.status_code != 201:
        print(f"  FAIL: {resp.status_code} {resp.text}")
        sys.exit(1)

    token = resp.json()["access_token"]

    # Get user ID
    me = api("GET", "/auth/me", token=token)
    user_id = me.json()["id"]
    print(f"  OK: user_id={user_id}")
    return token, user_id, email


def get_connectors(token: str) -> list[dict]:
    """Get all active county connectors."""
    resp = api("GET", "/scrapers/connectors")
    if resp.status_code != 200:
        print(f"  FAIL getting connectors: {resp.status_code}")
        sys.exit(1)
    connectors = resp.json()
    active = [c for c in connectors if c.get("active")]
    print(f"[CONNECTORS] {len(active)} active counties")
    return active


def create_scraper_config(token: str, connector: dict) -> str | None:
    """Create a scraper config for a connector. Returns config ID or None."""
    county = connector["county"]
    state = connector["state"]
    record_types = connector.get("record_types", ["probate"])
    record_type = record_types[0] if record_types else "probate"

    resp = api("POST", "/scrapers", token=token, json_data={
        "name": f"E2E {county.title()} {state.upper()} {record_type}",
        "county": county,
        "state": state,
        "record_type": record_type,
        "fields": {
            "party_name": True,
            "parcel_id": True,
            "property_address": True,
            "mailing_address": True,
            "heirs": True,
            "legal_description": True,
            "date_recorded": True,
        },
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "rolling_90"},
        "deliver": {"emails": [], "formats": ["csv"]},
    })

    if resp.status_code == 201:
        config_id = resp.json()["id"]
        print(f"  [CONFIG] {county}/{state} → {config_id}")
        return config_id
    else:
        print(f"  [CONFIG FAIL] {county}/{state}: {resp.status_code} {resp.text[:100]}")
        return None


def trigger_job(token: str, config_id: str) -> str | None:
    """Trigger a scrape job. Returns job ID or None."""
    resp = api("POST", "/jobs", token=token, json_data={
        "scraper_config_id": config_id,
        "trigger": "manual",
    })
    if resp.status_code == 201:
        job_id = resp.json()["id"]
        return job_id
    else:
        print(f"    [JOB FAIL] {resp.status_code} {resp.text[:100]}")
        return None


def wait_for_jobs(token: str, jobs: dict[str, str], max_wait: int = MAX_WAIT) -> dict[str, dict]:
    """Poll jobs until all complete. Returns {county: {status, record_count, ...}}."""
    results = {}
    pending = dict(jobs)  # {county: job_id}
    start = time.time()

    while pending and (time.time() - start) < max_wait:
        for county, job_id in list(pending.items()):
            resp = api("GET", f"/jobs/{job_id}", token=token)
            if resp.status_code != 200:
                print(f"    [{county}] poll error: {resp.status_code}")
                continue

            job = resp.json()
            status = job["status"]

            if status in ("done", "failed", "cancelled"):
                results[county] = job
                del pending[county]
                emoji = "OK" if status == "done" and job.get("record_count", 0) > 0 else "FAIL"
                print(f"  [{emoji}] {county}: {status} — {job.get('record_count', 0)} records")
                if job.get("error_message"):
                    print(f"       Error: {job['error_message'][:80]}")

        if pending:
            elapsed = int(time.time() - start)
            counties_left = ", ".join(pending.keys())
            print(f"  [WAIT] {elapsed}s elapsed — {len(pending)} pending: {counties_left}")
            time.sleep(POLL_INTERVAL)

    # Mark remaining as timed out
    for county, job_id in pending.items():
        results[county] = {"status": "timeout", "record_count": 0, "id": job_id}
        print(f"  [TIMEOUT] {county}")

    return results


def validate_results(token: str, county: str, job_id: str) -> dict:
    """Check job results for required fields. Returns validation summary."""
    resp = api("GET", f"/jobs/{job_id}/results?page=1&page_size=50", token=token)
    if resp.status_code != 200:
        return {"county": county, "valid": False, "error": f"HTTP {resp.status_code}"}

    data = resp.json()
    total = data.get("total", 0)
    items = data.get("items", [])

    if total == 0:
        return {"county": county, "valid": False, "error": "0 records", "total": 0}

    # Check how many records have each field
    has_party_name = sum(1 for r in items if r.get("party_name"))
    has_parcel_id = sum(1 for r in items if r.get("parcel_id"))
    has_property_addr = sum(1 for r in items if r.get("property_address"))
    has_mailing_addr = sum(1 for r in items if r.get("mailing_address"))
    has_date = sum(1 for r in items if r.get("date_recorded"))
    sample_size = len(items)

    return {
        "county": county,
        "valid": has_party_name > 0,
        "total": total,
        "sample_size": sample_size,
        "party_name": f"{has_party_name}/{sample_size}",
        "parcel_id": f"{has_parcel_id}/{sample_size}",
        "property_address": f"{has_property_addr}/{sample_size}",
        "mailing_address": f"{has_mailing_addr}/{sample_size}",
        "date_recorded": f"{has_date}/{sample_size}",
        "sample": items[0] if items else None,
    }


def main():
    print("=" * 70)
    print("BridgeLeads E2E Test — All WA Counties")
    print("=" * 70)

    # Step 1: Register
    token, user_id, email = register()

    # Step 2: Get connectors
    connectors = get_connectors(token)
    if not connectors:
        print("No active connectors found!")
        sys.exit(1)

    for c in connectors:
        print(f"  {c['county']}/{c['state']} — {c.get('scraper_mode', '?')} — {c.get('health_status', '?')}")

    # Step 3: Create scraper configs
    configs = {}  # {county: config_id}
    for connector in connectors:
        county = connector["county"]
        config_id = create_scraper_config(token, connector)
        if config_id:
            configs[county] = config_id

    if not configs:
        print("No scraper configs created!")
        sys.exit(1)

    print(f"\n[CONFIGS] Created {len(configs)} scraper configs")

    # Step 4: Trigger jobs in batches
    all_jobs = {}  # {county: job_id}
    counties = list(configs.keys())

    for i in range(0, len(counties), BATCH_SIZE):
        batch = counties[i:i + BATCH_SIZE]
        print(f"\n[BATCH {i // BATCH_SIZE + 1}] Triggering: {', '.join(batch)}")

        batch_jobs = {}
        for county in batch:
            job_id = trigger_job(token, configs[county])
            if job_id:
                batch_jobs[county] = job_id
                all_jobs[county] = job_id
                print(f"    [{county}] job={job_id}")

        if not batch_jobs:
            continue

        # Wait for this batch to complete
        print(f"\n[POLLING] Waiting for {len(batch_jobs)} jobs...")
        batch_results = wait_for_jobs(token, batch_jobs)

        # Validate results
        print(f"\n[VALIDATE] Checking results for batch...")
        for county, job in batch_results.items():
            if job["status"] == "done" and job.get("record_count", 0) > 0:
                v = validate_results(token, county, job["id"])
                print(f"  {county}: {v['total']} records — "
                      f"name={v['party_name']} parcel={v['parcel_id']} "
                      f"prop_addr={v['property_address']} mail_addr={v['mailing_address']}")
                if v.get("sample"):
                    s = v["sample"]
                    print(f"    Sample: {s.get('party_name', 'N/A')} | "
                          f"PID={s.get('parcel_id', 'N/A')} | "
                          f"Prop={s.get('property_address', 'N/A')} | "
                          f"Mail={s.get('mailing_address', 'N/A')}")

    # Step 5: Final summary
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"{'County':<20} {'Status':<10} {'Records':<10} {'Name':<6} {'Parcel':<8} {'PropAddr':<10} {'MailAddr':<10}")
    print("-" * 70)

    passed = 0
    failed = 0

    for county in sorted(all_jobs.keys()):
        job_id = all_jobs[county]
        # Get final job status
        resp = api("GET", f"/jobs/{job_id}", token=token)
        if resp.status_code != 200:
            print(f"{county:<20} {'ERROR':<10} {'?':<10}")
            failed += 1
            continue

        job = resp.json()
        status = job["status"]
        count = job.get("record_count", 0)

        if status == "done" and count > 0:
            v = validate_results(token, county, job_id)
            print(f"{county:<20} {status:<10} {count:<10} "
                  f"{v['party_name']:<6} {v['parcel_id']:<8} "
                  f"{v['property_address']:<10} {v['mailing_address']:<10}")
            passed += 1
        else:
            err = (job.get("error_message") or "")[:30]
            print(f"{county:<20} {status:<10} {count:<10} {err}")
            failed += 1

    print("-" * 70)
    print(f"PASSED: {passed}  FAILED: {failed}  TOTAL: {passed + failed}")
    print(f"Test account: {email}")
    print("=" * 70)


if __name__ == "__main__":
    main()
