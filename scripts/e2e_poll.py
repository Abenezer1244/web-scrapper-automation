"""Poll E2E test jobs and validate results.

Monitors all triggered jobs, waits for completion, and validates
that each county produces records with required fields.

Usage:
    python scripts/e2e_poll.py           # poll until all done
    python scripts/e2e_poll.py --status  # just show current status
"""

import json
import sys
import time

import requests

API = "https://api.bridgeleads.io"
POLL_INTERVAL = 30
MAX_WAIT = 7200  # 2 hours max


def api(path, token):
    return requests.get(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def validate_results(token, county, job_id):
    resp = api(f"/jobs/{job_id}/results?page=1&page_size=50", token)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    data = resp.json()
    total = data.get("total", 0)
    items = data.get("items", [])
    if total == 0:
        return {"total": 0}

    n = len(items)
    return {
        "total": total,
        "sample": n,
        "party_name": sum(1 for r in items if r.get("party_name")),
        "parcel_id": sum(1 for r in items if r.get("parcel_id")),
        "property_address": sum(1 for r in items if r.get("property_address")),
        "mailing_address": sum(1 for r in items if r.get("mailing_address")),
        "date_recorded": sum(1 for r in items if r.get("date_recorded")),
        "first_record": items[0] if items else None,
    }


def main():
    creds = json.load(open("scripts/.e2e_creds.json"))
    jobs = json.load(open("scripts/.e2e_jobs.json"))
    token = creds["token"]
    status_only = "--status" in sys.argv

    print(f"Monitoring {len(jobs)} jobs...")
    start = time.time()
    terminal = {}  # county -> job data
    last_status = {}

    while True:
        pending = {}
        for county, job_id in jobs.items():
            if county in terminal:
                continue
            resp = api(f"/jobs/{job_id}", token)
            if resp.status_code != 200:
                pending[county] = "error"
                continue
            job = resp.json()
            s = job["status"]
            rc = job.get("record_count", 0)

            if s in ("done", "failed", "cancelled"):
                terminal[county] = job
                tag = "DONE" if s == "done" and rc > 0 else "FAIL"
                print(f"  [{tag}] {county}: {s} — {rc} records")
                if job.get("error_message"):
                    print(f"       {job['error_message'][:100]}")
            else:
                pending[county] = s
                if last_status.get(county) != s:
                    print(f"  [..] {county}: {s}")
                    last_status[county] = s

        if not pending or status_only:
            break

        elapsed = int(time.time() - start)
        if elapsed > MAX_WAIT:
            print(f"\nTIMEOUT after {elapsed}s")
            for c in pending:
                terminal[c] = {"status": "timeout", "record_count": 0, "id": jobs[c]}
            break

        print(f"\n  --- {elapsed}s elapsed — {len(terminal)} done, {len(pending)} pending ---")
        time.sleep(POLL_INTERVAL)

    # Final validation
    print("\n" + "=" * 90)
    print("FINAL RESULTS")
    print("=" * 90)
    header = f"{'County':<20} {'Status':<10} {'Records':<8} {'Name':<6} {'Parcel':<8} {'PropAddr':<10} {'MailAddr':<10}"
    print(header)
    print("-" * 90)

    passed = 0
    failed = 0
    for county in sorted(jobs.keys()):
        job = terminal.get(county)
        if not job:
            print(f"{county:<20} {'pending':<10}")
            failed += 1
            continue

        s = job["status"]
        rc = job.get("record_count", 0)

        if s == "done" and rc > 0:
            v = validate_results(token, county, job["id"])
            n = v.get("sample", 0)
            print(f"{county:<20} {s:<10} {rc:<8} "
                  f"{v.get('party_name',0)}/{n:<3} "
                  f"{v.get('parcel_id',0)}/{n:<5} "
                  f"{v.get('property_address',0)}/{n:<7} "
                  f"{v.get('mailing_address',0)}/{n}")
            if v.get("first_record"):
                r = v["first_record"]
                print(f"  -> {r.get('party_name','?')} | PID={r.get('parcel_id','?')} | "
                      f"Prop={r.get('property_address','?')} | Mail={r.get('mailing_address','?')}")
            passed += 1
        else:
            err = (job.get("error_message") or "")[:40]
            print(f"{county:<20} {s:<10} {rc:<8} {err}")
            failed += 1

    print("-" * 90)
    print(f"PASSED: {passed}  FAILED: {failed}  TOTAL: {passed + failed}")


if __name__ == "__main__":
    main()
