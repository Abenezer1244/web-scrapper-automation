"""E2E: Test all 3 Pierce County record types via app.bridgeleads.io in live Chrome.

Creates configs if missing, clicks View > then Run, monitors each job.
"""

import asyncio
import re
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _creds import fixture_creds


async def create_config_if_needed(token: str, name: str, county: str, state: str, record_type: str) -> str:
    """Create scraper config via API if it doesn't exist, return config ID."""
    import httpx
    async with httpx.AsyncClient() as client:
        # Check existing
        resp = await client.get(
            "https://api.bridgeleads.io/scrapers",
            headers={"Authorization": f"Bearer {token}"},
        )
        for c in resp.json():
            if c["county"] == county and c["record_type"] == record_type:
                return c["id"]

        # Create new
        resp = await client.post(
            "https://api.bridgeleads.io/scrapers",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": name, "county": county, "state": state, "record_type": record_type,
                  "schedule": {"frequency": "manual", "date_range_mode": "rolling_90"}},
        )
        return resp.json()["id"]


async def run_job_and_wait(token: str, config_id: str, label: str) -> dict:
    """Create job via API and poll until done."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.bridgeleads.io/jobs",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"scraper_config_id": config_id, "trigger": "manual"},
        )
        data = resp.json()
        job_id = data.get("id")
        if not job_id:
            print(f"  FAILED to create job: {data.get('detail', '?')}")
            return {"status": "failed", "record_count": 0}

        print(f"  Job {job_id[:8]} created")

        # Poll
        for _ in range(60):
            await asyncio.sleep(15)
            resp = await client.get(
                f"https://api.bridgeleads.io/jobs/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            d = resp.json()
            status = d["status"]
            records = d["record_count"]
            elapsed = d.get("elapsed_time", "?")

            if status in ("done", "failed"):
                return d

        return d


async def main():
    from playwright.async_api import async_playwright
    import httpx

    # Get API token
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.bridgeleads.io/auth/login",
            json=dict(zip(("email", "password"), fixture_creds())),
        )
        token = resp.json()["access_token"]

    # Ensure all 3 Pierce configs exist
    configs = {}
    for rt, name in [("probate", "Pierce Probate"), ("pre_foreclosure", "Pierce Pre-Foreclosure"), ("divorce", "Pierce Divorce")]:
        cid = await create_config_if_needed(token, name, "pierce", "wa", rt)
        configs[rt] = cid
        print(f"Config {rt}: {cid[:8]}")

    # Open Chrome
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome", slow_mo=200)
        page = await browser.new_page()

        # Login
        await page.goto("https://app.bridgeleads.io/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        _fix_email, _fix_pass = fixture_creds()
        await page.locator('input[type="email"]').first.fill(_fix_email)
        await page.locator('input[type="password"]').first.fill(_fix_pass)
        await page.locator('button[type="submit"]').first.click()
        await page.wait_for_timeout(5000)
        print(f"\nDashboard loaded: {page.url}")

        # Test each record type
        results_summary = {}

        for rt in ["probate", "pre_foreclosure", "divorce"]:
            label = rt.replace("_", " ").title()
            print(f"\n{'='*50}")
            print(f"  Pierce County {label}")
            print(f"{'='*50}")

            # Run the job via API (scraping happens on Railway)
            t0 = time.time()
            job_result = await run_job_and_wait(token, configs[rt], label)
            elapsed = round(time.time() - t0)

            status = job_result["status"]
            records = job_result["record_count"]
            print(f"  {status.upper()} | {records} records | {elapsed}s")

            results_summary[rt] = {"status": status, "records": records, "elapsed": elapsed}

            # Navigate to the live run page to show it in Chrome
            job_id = job_result.get("id", "")
            if job_id:
                await page.goto(f"https://app.bridgeleads.io/live/{job_id}", wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
                await page.screenshot(path=f"e2e_pierce_{rt}_result.png")

        # Show results page
        await page.goto("https://app.bridgeleads.io/results", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path="e2e_pierce_results.png")

        # Summary
        print(f"\n{'='*50}")
        print(f"  PIERCE COUNTY SUMMARY")
        print(f"{'='*50}")
        for rt, data in results_summary.items():
            label = rt.replace("_", " ").title()
            print(f"  {label:20s}: {data['status']:8s} | {data['records']:4d} records | {data['elapsed']}s")

        # Check enrichment quality from API
        print(f"\n  Enrichment quality:")
        async with httpx.AsyncClient(timeout=30) as client:
            for rt, data in results_summary.items():
                if data["records"] == 0:
                    continue
                # Get job results
                resp = await client.get(
                    f"https://api.bridgeleads.io/jobs/{data.get('id', results_summary[rt].get('id',''))}/results?page=1&page_size=999",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    continue
                items = resp.json().get("items", [])
                total = len(items)
                if total == 0:
                    continue
                wp = sum(1 for r in items if r.get("property_address"))
                wm = sum(1 for r in items if r.get("mailing_address"))
                label = rt.replace("_", " ").title()
                print(f"  {label:20s}: prop={wp}/{total} ({100*wp//total}%) | mail={wm}/{total} ({100*wm//total}%)")

        print(f"\nBrowser open for 60s...")
        await page.wait_for_timeout(60000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
