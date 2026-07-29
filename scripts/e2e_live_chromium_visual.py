"""Live Chrome visual E2E test — opens a VISIBLE browser window and walks
through the entire BridgeLeads flow: login, create scraper, watch scrape,
view results, download CSV."""

import asyncio
import re
import time
import uuid

import requests as req
from _creds import test_password
from playwright.async_api import async_playwright

API = "https://api.bridgeleads.io"


async def live_scrape():
    # Pre-register via API so we have a valid account
    email = f"live_demo_{uuid.uuid4().hex[:6]}@bridgeleads.io"
    password = test_password()
    print("=" * 60)
    print("LIVE CHROME E2E TEST")
    print("=" * 60)
    print(f"\nPre-registering: {email}")
    r = req.post(f"{API}/auth/register", json={"email": email, "password": password}, timeout=10)
    assert r.status_code == 201, f"Register failed: {r.text[:100]}"
    api_token = r.json()["access_token"]
    print("  API token obtained")

    # Pre-create scraper config via API
    r = req.post(f"{API}/scrapers", headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}, json={
        "name": "Pierce County Live Demo",
        "county": "pierce", "state": "wa", "record_type": "probate",
        "fields": {"party_name": True, "parcel_id": True, "property_address": True, "mailing_address": True, "heirs": True, "legal_description": True, "date_recorded": True},
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "custom", "date_from": "2026-03-01", "date_to": "2026-03-28"},
        "deliver": {"emails": [], "formats": ["csv"]},
    }, timeout=10)
    config_id = r.json()["id"]
    print(f"  Scraper config: {config_id}")

    # Trigger the job via API
    r = req.post(f"{API}/jobs", headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}, json={"scraper_config_id": config_id, "trigger": "manual"}, timeout=10)
    job_id = r.json()["id"]
    print(f"  Job triggered: {job_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=150, )
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # 1. Login via the UI
        print("\n[1/8] Logging in via Chrome...")
        await page.goto("https://bridgeleads.io/login")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)
        await page.locator('input[name="email"]').fill(email)
        await page.locator('input[name="password"]').fill(password)
        await page.screenshot(path="live_01_login.png")
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(4000)
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path="live_02_dashboard.png")
        print(f"  Logged in. URL: {page.url}")

        # 2. Dashboard
        print("[2/8] Dashboard...")
        await page.goto("https://bridgeleads.io/dashboard")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="live_03_dashboard_loaded.png")
        print("  Dashboard loaded")

        # 3. Navigate to live page to watch scrape
        print(f"[3/8] Watching live scrape at /live/{job_id}...")
        await page.goto(f"https://bridgeleads.io/live/{job_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="live_04_scraping_start.png")

        # 4. Poll and watch
        print("[4/8] Monitoring progress...")
        start = time.time()
        last_log = ""
        screenshot_num = 5

        while time.time() - start < 300:
            await page.wait_for_timeout(5000)

            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""

            elapsed = int(time.time() - start)

            if "Complete" in body:
                await page.screenshot(path=f"live_{screenshot_num:02d}_complete.png")
                print(f"  [{elapsed}s] COMPLETE!")
                break

            if "Failed" in body:
                await page.screenshot(path=f"live_{screenshot_num:02d}_failed.png")
                print(f"  [{elapsed}s] FAILED")
                break

            m = re.search(r"Page (\d+) of (\d+)", body)
            status = f"page {m.group(1)}/{m.group(2)}" if m else "processing"

            m2 = re.search(r"(\d+)\s*records?", body, re.IGNORECASE)
            rec = m2.group(1) if m2 else "?"

            log = f"{status} ({rec} records)"
            if log != last_log:
                print(f"  [{elapsed}s] {log}")
                last_log = log
                screenshot_num += 1
                await page.screenshot(path=f"live_{screenshot_num:02d}_progress.png")

        # 5. View scrapers
        print("[5/8] Checking scrapers page...")
        await page.goto("https://bridgeleads.io/scrapers")
        await page.wait_for_timeout(2500)
        await page.screenshot(path="live_20_scrapers.png")
        print("  Scrapers loaded")

        # 6. View results list
        print("[6/8] Checking results list...")
        await page.goto("https://bridgeleads.io/results")
        await page.wait_for_timeout(2500)
        await page.screenshot(path="live_21_results_list.png")

        # Click into first result
        link = page.locator("a[href*='/results/']").first
        if await link.count() > 0:
            await link.click()
            await page.wait_for_timeout(3000)
        else:
            await page.goto(f"https://bridgeleads.io/results/{job_id}")
            await page.wait_for_timeout(3000)
        await page.screenshot(path="live_22_results_detail.png")
        print("  Results detail loaded")

        # 7. Download
        print("[7/8] Downloading CSV...")
        dl_btn = page.locator("button:has-text('Download'), a:has-text('Download')")
        if await dl_btn.count() > 0:
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await dl_btn.first.click()
                download = await dl_info.value
                fname = download.suggested_filename
                await download.save_as(f"scripts/live_download_{fname}")
                print(f"  Downloaded: {fname}")

                # Show first few lines
                with open(f"scripts/live_download_{fname}", errors="replace") as f:
                    lines = f.readlines()
                print(f"  Rows: {len(lines) - 1}")
                print(f"  Header: {lines[0].strip()[:80]}")
                if len(lines) > 1:
                    print(f"  Row 1:  {lines[1].strip()[:80]}")
            except Exception as e:
                print(f"  Download error: {e}")
        else:
            print("  No download button visible")

        await page.screenshot(path="live_23_after_download.png")

        # 8. Settings + deliver
        print("[8/8] Checking remaining pages...")
        for name, path in [("deliver", "/deliver"), ("settings", "/settings")]:
            await page.goto(f"https://bridgeleads.io{path}")
            await page.wait_for_timeout(2000)
            await page.screenshot(path=f"live_24_{name}.png")
            print(f"  {name}: OK")

        print("\n" + "=" * 60)
        print("LIVE CHROME E2E TEST COMPLETE")
        print(f"  Email:    {email}")
        print(f"  Job ID:   {job_id}")
        print("  Screenshots: live_*.png")
        print("=" * 60)

        await page.wait_for_timeout(3000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(live_scrape())
