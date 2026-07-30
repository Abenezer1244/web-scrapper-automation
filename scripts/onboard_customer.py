#!/usr/bin/env python3
"""
BridgeLeads customer onboarding script.
Creates a user account, scraper config, and runs the first job.

Usage:
    python scripts/onboard_customer.py \
        --api-url https://api.bridgeleads.io \
        --email mike@example.com \
        --password "SecurePass123!" \
        --plan pro
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


_TIMEOUT = 30


def _urlopen(req: urllib.request.Request):
    """urlopen with a timeout and an http(s)-only guard.

    --api-url is operator-supplied, so a typo'd or pasted `file:`/`ftp:` URL would
    otherwise be opened as-is. Pinning the scheme keeps this to the one thing it is
    meant to do, and the timeout stops a hung API from hanging onboarding forever.
    """
    if req.type not in ("http", "https"):
        raise SystemExit(f"Refusing non-HTTP(S) URL: {req.full_url}")
    return urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 — scheme checked above


def _post(url: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310 — inert construction; _urlopen() enforces http(s)
    try:
        with _urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ERROR {e.code}: {body}")
        sys.exit(1)


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 — inert construction; _urlopen() enforces http(s)
    with _urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Onboard a BridgeLeads customer")
    parser.add_argument("--api-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--email", required=True, help="Customer email")
    parser.add_argument("--password", required=True, help="Customer password")
    parser.add_argument("--plan", default="starter", choices=["starter", "pro", "business", "agency"])
    parser.add_argument("--county", default="pierce", help="County to set up (default: pierce)")
    parser.add_argument("--state", default="WA", help="State (default: WA)")
    parser.add_argument("--record-type", default="probate", help="Record type (default: probate)")
    parser.add_argument("--delivery-email", help="Email for lead delivery (defaults to account email)")
    parser.add_argument("--run-now", action="store_true", help="Trigger a manual job immediately")
    args = parser.parse_args()

    base = args.api_url.rstrip("/")
    delivery_email = args.delivery_email or args.email

    print(f"\n{'='*60}")
    print("  BridgeLeads Customer Onboarding")
    print(f"{'='*60}")
    print(f"  API:    {base}")
    print(f"  Email:  {args.email}")
    print(f"  Plan:   {args.plan}")
    print(f"  County: {args.county.upper()}, {args.state}")
    print(f"  Type:   {args.record_type}")
    print(f"{'='*60}\n")

    # ── Step 1: Register ──────────────────────────────────────────────────────
    print("1. Creating account...")
    _post(f"{base}/auth/register", {
        "email": args.email,
        "password": args.password,
    })
    print(f"   ✓ Account created: {args.email}")

    # ── Step 2: Login ─────────────────────────────────────────────────────────
    print("2. Logging in...")
    login = _post(f"{base}/auth/login", {
        "email": args.email,
        "password": args.password,
    })
    token = login["access_token"]
    print("   ✓ Authenticated (token acquired)")

    # ── Step 3: Verify connector exists ───────────────────────────────────────
    print(f"3. Verifying connector: {args.county}/{args.state}/{args.record_type}...")
    req = urllib.request.Request(f"{base}/scrapers/connectors")  # noqa: S310 — inert construction; _urlopen() enforces http(s)
    with _urlopen(req) as resp:
        connectors = json.loads(resp.read())

    match = next(
        (c for c in connectors
         if c["county"].lower() == args.county.lower()
         and c["state"].upper() == args.state.upper()
         and args.record_type in c["record_types"]),
        None
    )
    if not match:
        print(f"   ERROR: No active connector for {args.county}/{args.state}/{args.record_type}")
        print("   Available connectors:")
        for c in connectors:
            print(f"     - {c['county']}/{c['state']}: {c['record_types']}")
        sys.exit(1)
    print(f"   ✓ Connector found (health: {match['health_status']})")

    # ── Step 4: Create scraper config ─────────────────────────────────────────
    print("4. Creating scraper config...")
    schedule_config = {
        "frequency": "daily",
        "time": "06:00",
        "range_mode": "rolling_90"
    }
    deliver_config = {
        "format": "csv",
        "emails": [delivery_email],
        "webhook_url": None
    }
    config = _post(f"{base}/scrapers", {
        "name": f"{args.county.title()} County {args.state} {args.record_type.title()}",
        "county": args.county.lower(),
        "state": args.state.upper(),
        "record_type": args.record_type,
        "fields": ["party_name", "parcel_id", "property_address", "mailing_address", "heirs", "legal_description", "date_recorded"],
        "enrichment": ["property_address"],
        "schedule": schedule_config,
        "deliver": deliver_config,
    }, token=token)
    print(f"   ✓ Scraper config created: {config['name']} (id: {config['id'][:8]}...)")
    print("   ✓ Daily schedule set: 6:00 AM UTC")
    print(f"   ✓ Delivery email: {delivery_email}")

    # ── Step 5: Optional manual run ───────────────────────────────────────────
    if args.run_now:
        print("5. Triggering manual job...")
        job = _post(f"{base}/jobs", {
            "scraper_config_id": config["id"],
            "trigger": "manual"
        }, token=token)
        job_id = job["id"]
        print(f"   ✓ Job created: {job_id[:8]}... (status: {job['status']})")

        print("   Polling job status (press Ctrl+C to stop)...")
        terminal = {"done", "failed", "cancelled"}
        for _ in range(60):  # Poll up to 5 minutes
            time.sleep(5)
            j = _get(f"{base}/jobs/{job_id}", token)
            status = j["status"]
            count = j.get("record_count", 0)
            print(f"   [{status}] {count} records", end="\r")
            if status in terminal:
                print()
                if status == "done":
                    print(f"   ✓ Job complete — {count} records found")
                    print(f"   ✓ Export key: {j.get('export_key', 'N/A')}")
                else:
                    print(f"   ✗ Job {status}: {j.get('error_message', 'unknown error')}")
                break
        else:
            print(f"\n   ! Job still running after 5 minutes — check /live/{job_id}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  Onboarding complete!")
    print(f"{'='*60}")
    print(f"  Account:   {args.email}")
    print(f"  Plan:      {args.plan}")
    print(f"  Scraper:   {config['name']}")
    print("  Schedule:  Daily at 6:00 AM UTC")
    print(f"  Delivery:  {delivery_email}")
    if args.plan in ("pro", "business", "agency"):
        print("\n  Next: Collect payment via Stripe checkout")
        print(f"  POST {base}/billing/checkout  {{price_id: STRIPE_PRICE_{args.plan.upper()}}}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
