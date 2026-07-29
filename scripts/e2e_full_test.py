"""Full E2E test: register, create config, scrape Pierce County, validate
results, download CSV, and test all frontend pages with Chromium."""

import asyncio
import time
import uuid

import requests
from playwright.async_api import async_playwright

from _creds import test_password

API = "https://api.bridgeleads.io"


def api(method, path, token=None, data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(
        method, f"{API}{path}", headers=headers, json=data, timeout=30
    )


async def full_e2e():
    print("=" * 70)
    print("BridgeLeads Full E2E Test")
    print("=" * 70)

    # 1. Register
    email = f"e2e_full_{uuid.uuid4().hex[:6]}@bridgeleads.io"
    password = test_password()
    print(f"\n[1/8] Registering: {email}")
    r = api("POST", "/auth/register", data={"email": email, "password": password})
    assert r.status_code == 201, f"Register failed: {r.status_code} {r.text[:100]}"
    token = r.json()["access_token"]
    me = api("GET", "/auth/me", token=token).json()
    print(f"  OK: {me['email']} (plan: {me['plan']})")

    # 2. Create scraper config
    print(f"\n[2/8] Creating Pierce County scraper config...")
    r = api(
        "POST",
        "/scrapers",
        token=token,
        data={
            "name": "E2E Full Test Pierce",
            "county": "pierce",
            "state": "wa",
            "record_type": "probate",
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
            "schedule": {
                "frequency": "manual",
                "date_range_mode": "custom",
                "date_from": "2026-03-01",
                "date_to": "2026-03-28",
            },
            "deliver": {"emails": [], "formats": ["csv"]},
        },
    )
    assert r.status_code == 201, f"Config failed: {r.status_code} {r.text[:100]}"
    config_id = r.json()["id"]
    print(f"  OK: config_id={config_id}")

    # 3. List scrapers
    print(f"\n[3/8] Listing scrapers...")
    r = api("GET", "/scrapers", token=token)
    scrapers = r.json()
    print(f"  OK: {len(scrapers)} scraper(s)")

    # 4. Trigger job
    print(f"\n[4/8] Triggering scrape job...")
    r = api(
        "POST",
        "/jobs",
        token=token,
        data={"scraper_config_id": config_id, "trigger": "manual"},
    )
    assert r.status_code == 201, f"Job failed: {r.status_code} {r.text[:100]}"
    job_id = r.json()["id"]
    print(f"  OK: job_id={job_id}")

    # 5. Poll job
    print(f"\n[5/8] Polling job progress...")
    start = time.time()
    status = "pending"
    rc = 0
    while time.time() - start < 600:
        r = api("GET", f"/jobs/{job_id}", token=token)
        j = r.json()
        status = j["status"]
        rc = j.get("record_count", 0)
        label = j.get("progress_label", "-")
        eta = j.get("estimated_time_remaining", "-")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] {status:<10} {str(label):<30} rec={rc}  eta={eta}")
        if status in ("done", "failed", "cancelled"):
            break
        time.sleep(10)

    assert status == "done", f"Job ended with status={status}"
    print(f"  SCRAPE PASSED: {rc} records")

    # 6. Get results
    print(f"\n[6/8] Fetching results...")
    r = api("GET", f"/jobs/{job_id}/results?page=1&page_size=5", token=token)
    assert r.status_code == 200, f"Results failed: {r.status_code}"
    data = r.json()
    items = data.get("items", [])
    enriched = data.get("enriched_count", 0)
    print(f"  OK: {data['total']} total, {enriched} enriched, showing {len(items)}")
    for i, rec in enumerate(items[:3]):
        name = rec.get("party_name", "?")
        pid = rec.get("parcel_id", "?")
        addr = rec.get("property_address", "?")
        mail = rec.get("mailing_address", "?")
        print(f"    [{i+1}] {name} | PID={pid}")
        print(f"         Prop: {addr}")
        print(f"         Mail: {mail}")

    # 7. Download CSV
    print(f"\n[7/8] Testing CSV download...")
    download_ok = False
    r = api("GET", f"/jobs/{job_id}/export-url", token=token)
    if r.status_code == 200:
        url = r.json()["url"]
        if url.startswith("/"):
            url = f"{API}{url}"
        if "?" in url:
            url += f"&token={token}"
        else:
            url += f"?token={token}"

        dl = requests.get(url, timeout=30)
        print(f"  Status: {dl.status_code}")
        print(f"  Content-Type: {dl.headers.get('content-type', '?')}")
        print(f"  Size: {len(dl.content)} bytes")

        if dl.status_code == 200 and len(dl.content) > 50:
            with open("scripts/e2e_download.csv", "wb") as f:
                f.write(dl.content)
            lines = dl.text.strip().split("\n")
            print(f"  Rows: {len(lines) - 1} data rows (+ header)")
            print(f"  Header: {lines[0][:100]}")
            if len(lines) > 1:
                print(f"  Row 1:  {lines[1][:100]}")
            download_ok = True
            print(f"  DOWNLOAD PASSED")
        else:
            print(f"  DOWNLOAD FAILED: {dl.text[:200]}")
    else:
        print(f"  Export URL failed: {r.status_code} {r.text[:100]}")

    # 8. Frontend pages
    print(f"\n[8/8] Testing frontend pages with Chromium...")
    pages_ok = 0
    pages_fail = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        await page.goto("https://bridgeleads.io/login")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await page.locator('input[name="email"]').fill(email)
        await page.locator('input[name="password"]').fill(password)
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(4000)

        tests = [
            ("Dashboard", "/dashboard"),
            ("Scrapers", "/scrapers"),
            ("Results", "/results"),
            ("Deliver", "/deliver"),
            ("Settings", "/settings"),
            ("Live Run", f"/live/{job_id}"),
            ("Results Detail", f"/results/{job_id}"),
        ]

        for name, path in tests:
            try:
                await page.goto(f"https://bridgeleads.io{path}")
                await page.wait_for_timeout(2500)
                content = await page.content()
                has_error = (
                    "Application error" in content
                    or "Internal Server Error" in content
                )
                if has_error:
                    print(f"  FAIL: {name}")
                    pages_fail += 1
                else:
                    print(f"  OK:   {name}")
                    pages_ok += 1
                    await page.screenshot(
                        path=f"scripts/e2e_page_{name.lower().replace(' ', '_')}.png"
                    )
            except Exception as e:
                print(f"  FAIL: {name} - {str(e)[:60]}")
                pages_fail += 1

        await browser.close()

    # Summary
    print(f"\n{'=' * 70}")
    print(f"E2E TEST COMPLETE")
    print(f"  Register:  PASS")
    print(f"  Config:    PASS (config_id={config_id[:8]})")
    print(f"  Scrape:    PASS ({rc} records)")
    print(f"  Results:   PASS ({enriched} enriched)")
    print(f"  Download:  {'PASS' if download_ok else 'FAIL'}")
    print(f"  Pages:     {pages_ok}/{len(tests)} OK")
    all_pass = status == "done" and download_ok and pages_fail == 0
    print(f"\n  {'ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED'}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(full_e2e())
