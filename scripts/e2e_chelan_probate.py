"""Chelan probate scrape — visible Chrome, admin login, watch live."""

import asyncio
import re
import time

import requests as req
from _creds import admin_creds
from playwright.async_api import async_playwright

API = "https://api.bridgeleads.io"
FRONTEND = "https://bridgeleads.io"
EMAIL, PASSWORD = admin_creds()


async def run():
    print("=" * 60)
    print("CHELAN PROBATE SCRAPE — LIVE CHROME")
    print("=" * 60)

    # 1. Get API token
    print("\n[1] Logging in via API...")
    r = req.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print("  Token obtained")

    # 2. Find existing Chelan probate config or create one
    print("[2] Looking for existing Chelan probate config...")
    r = req.get(f"{API}/scrapers", headers=headers, timeout=10)
    assert r.status_code == 200, f"List scrapers failed: {r.status_code} {r.text[:200]}"
    configs = r.json()
    chelan = [c for c in configs if c["county"] == "chelan" and c["record_type"] == "probate"]
    if chelan:
        config_id = chelan[0]["id"]
        print(f"  Found existing: {config_id} ({chelan[0]['name']})")
    else:
        print("  None found, creating new...")
        r = req.post(f"{API}/scrapers", headers=headers, json={
            "name": "Chelan Probate E2E",
            "county": "chelan", "state": "wa", "record_type": "probate",
            "fields": {
                "party_name": True, "parcel_id": True, "property_address": True,
                "mailing_address": True, "heirs": True, "legal_description": True,
                "date_recorded": True
            },
            "enrichment": {"property_lookup": True, "skip_tracing": False},
            "schedule": {
                "frequency": "manual",
                "date_range_mode": "custom",
                "date_from": "2026-03-15",
                "date_to": "2026-04-14"
            },
            "deliver": {"emails": [], "formats": ["csv"]},
        }, timeout=10)
        assert r.status_code in (200, 201), f"Create failed: {r.status_code} {r.text[:200]}"
        config_id = r.json()["id"]
        print(f"  Created: {config_id}")

    # 3. Trigger job
    print("[3] Triggering job...")
    r = req.post(f"{API}/jobs", headers=headers, json={
        "scraper_config_id": config_id, "trigger": "manual"
    }, timeout=10)
    assert r.status_code in (200, 201), f"Job trigger failed: {r.status_code} {r.text[:200]}"
    job_id = r.json()["id"]
    print(f"  Job: {job_id}")

    # 4. Open visible browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Login via UI
        print("\n[4] Logging in via Chrome...")
        await page.goto(f"{FRONTEND}/login")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)
        await page.locator('input[name="email"]').fill(EMAIL)
        await page.locator('input[name="password"]').fill(PASSWORD)
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path="scripts/e2e_screenshots/chelan_01_dashboard.png")
        print(f"  Logged in. URL: {page.url}")

        # Watch live scrape
        print("[5] Watching live scrape...")
        await page.goto(f"{FRONTEND}/live/{job_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="scripts/e2e_screenshots/chelan_02_scraping.png")

        start = time.time()
        last_log = ""
        shot = 3

        while time.time() - start < 600:  # 10 min max
            await page.wait_for_timeout(5000)
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""

            elapsed = int(time.time() - start)

            if "Complete" in body or "complete" in body:
                await page.screenshot(path=f"scripts/e2e_screenshots/chelan_{shot:02d}_complete.png")
                print(f"  [{elapsed}s] COMPLETE!")
                break

            if "Failed" in body or "failed" in body:
                await page.screenshot(path=f"scripts/e2e_screenshots/chelan_{shot:02d}_failed.png")
                print(f"  [{elapsed}s] FAILED")
                break

            m = re.search(r"(\d+)\s*records?", body, re.IGNORECASE)
            rec = m.group(1) if m else "?"
            log = f"{rec} records"
            if log != last_log:
                print(f"  [{elapsed}s] {log}")
                last_log = log
                shot += 1
                await page.screenshot(path=f"scripts/e2e_screenshots/chelan_{shot:02d}_progress.png")
        else:
            print("  TIMEOUT after 600s")

        # View results
        print("[6] Viewing results...")
        await page.goto(f"{FRONTEND}/results/{job_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=f"scripts/e2e_screenshots/chelan_{shot+1:02d}_results.png")

        # Try to expand a row
        row = page.locator("tr.cursor-pointer, tr[data-expandable]").first
        if await row.count() > 0:
            await row.click()
            await page.wait_for_timeout(1500)
            await page.screenshot(path=f"scripts/e2e_screenshots/chelan_{shot+2:02d}_expanded.png")
            print("  Expanded row detail")

        # Download
        print("[7] Downloading CSV...")
        dl_btn = page.locator("button:has-text('Download'), a:has-text('Download')")
        if await dl_btn.count() > 0:
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await dl_btn.first.click()
                download = await dl_info.value
                fname = download.suggested_filename
                await download.save_as(f"scripts/{fname}")
                print(f"  Downloaded: {fname}")
                with open(f"scripts/{fname}", errors="replace") as f:
                    lines = f.readlines()
                print(f"  Rows: {len(lines) - 1}")
                if lines:
                    print(f"  Header: {lines[0].strip()[:100]}")
                if len(lines) > 1:
                    print(f"  Row 1:  {lines[1].strip()[:100]}")
            except Exception as e:
                print(f"  Download error: {e}")
        else:
            print("  No download button found")

        print("\n" + "=" * 60)
        print("CHELAN PROBATE E2E COMPLETE")
        print(f"  Job: {job_id}")
        print("  Screenshots: scripts/e2e_screenshots/chelan_*.png")
        print("=" * 60)

        await page.wait_for_timeout(5000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
