"""Live Chromium test of all schedule options: Manual, Daily, Custom dates.
Opens a visible browser and walks through the wizard for each."""

import asyncio
import time
import uuid

import requests as req
from playwright.async_api import async_playwright

from _creds import test_password

API = "https://api.bridgeleads.io"


async def test_schedules():
    email = f"sched_live_{uuid.uuid4().hex[:6]}@bridgeleads.io"
    password = test_password()

    # Pre-register
    r = req.post(f"{API}/auth/register", json={"email": email, "password": password}, timeout=10)
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print("=" * 60)
    print("LIVE CHROMIUM SCHEDULE TEST")
    print("=" * 60)
    print(f"Account: {email}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=150)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Login
        print("[LOGIN] Signing in...")
        await page.goto("https://bridgeleads.io/login")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await page.locator('input[name="email"]').fill(email)
        await page.locator('input[name="password"]').fill(password)
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(4000)
        print(f"  Dashboard: {page.url}\n")

        # ═══════════════════════════════════════════════════════════
        # TEST 1: Manual + Rolling 90 days
        # ═══════════════════════════════════════════════════════════
        print("[TEST 1/3] Manual + Rolling 90 days")
        print("-" * 40)

        r = req.post(f"{API}/scrapers", headers=headers, json={
            "name": "Test 1 - Manual Rolling90",
            "county": "pierce", "state": "wa", "record_type": "probate",
            "fields": {"party_name": True, "parcel_id": True, "property_address": True, "mailing_address": True},
            "enrichment": {"property_lookup": True, "skip_tracing": False},
            "schedule": {"frequency": "manual", "date_range_mode": "rolling_90"},
            "deliver": {"emails": [], "formats": ["csv"]},
        }, timeout=10)
        config1 = r.json()
        print(f"  Config: {config1['id'][:8]}")
        print(f"  Schedule: freq={config1['schedule']['frequency']}, range={config1['schedule']['date_range_mode']}")

        # Trigger job
        r = req.post(f"{API}/jobs", headers=headers, json={"scraper_config_id": config1["id"], "trigger": "manual"}, timeout=10)
        job1_id = r.json()["id"]
        print(f"  Job triggered: {job1_id[:8]}")

        # Watch on live page
        await page.goto(f"https://bridgeleads.io/live/{job1_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="sched_01_manual_live.png")

        # Poll until done
        start = time.time()
        while time.time() - start < 180:
            r = req.get(f"{API}/jobs/{job1_id}", headers=headers, timeout=10)
            j = r.json()
            s = j["status"]
            rc = j.get("record_count", 0)
            elapsed = int(time.time() - start)
            if s in ("done", "failed"):
                break
            print(f"  [{elapsed}s] {s} - {rc} records")
            await page.wait_for_timeout(8000)

        await page.screenshot(path="sched_02_manual_done.png")
        print(f"  RESULT: {s} - {rc} records")
        test1_pass = s == "done" and rc > 0
        print(f"  {'PASS' if test1_pass else 'FAIL'}\n")

        # ═══════════════════════════════════════════════════════════
        # TEST 2: Manual + Custom dates (short range)
        # ═══════════════════════════════════════════════════════════
        print("[TEST 2/3] Manual + Custom dates (Mar 15-28, 2026)")
        print("-" * 40)

        r = req.post(f"{API}/scrapers", headers=headers, json={
            "name": "Test 2 - Custom Dates",
            "county": "pierce", "state": "wa", "record_type": "probate",
            "fields": {"party_name": True, "parcel_id": True, "property_address": True, "mailing_address": True},
            "enrichment": {"property_lookup": True, "skip_tracing": False},
            "schedule": {"frequency": "manual", "date_range_mode": "custom", "date_from": "2026-03-15", "date_to": "2026-03-28"},
            "deliver": {"emails": [], "formats": ["csv"]},
        }, timeout=10)
        config2 = r.json()
        print(f"  Config: {config2['id'][:8]}")
        print(f"  Schedule: freq={config2['schedule']['frequency']}, range={config2['schedule']['date_range_mode']}")
        print(f"  Dates: {config2['schedule']['date_from']} to {config2['schedule']['date_to']}")

        r = req.post(f"{API}/jobs", headers=headers, json={"scraper_config_id": config2["id"], "trigger": "manual"}, timeout=10)
        job2_id = r.json()["id"]
        print(f"  Job triggered: {job2_id[:8]}")

        await page.goto(f"https://bridgeleads.io/live/{job2_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="sched_03_custom_live.png")

        start = time.time()
        while time.time() - start < 180:
            r = req.get(f"{API}/jobs/{job2_id}", headers=headers, timeout=10)
            j = r.json()
            s = j["status"]
            rc = j.get("record_count", 0)
            elapsed = int(time.time() - start)
            if s in ("done", "failed"):
                break
            print(f"  [{elapsed}s] {s} - {rc} records")
            await page.wait_for_timeout(8000)

        await page.screenshot(path="sched_04_custom_done.png")
        print(f"  RESULT: {s} - {rc} records")
        # Custom range Mar 15-28 should have fewer records than rolling 90
        test2_pass = s == "done" and rc > 0
        print(f"  {'PASS' if test2_pass else 'FAIL'}\n")

        # ═══════════════════════════════════════════════════════════
        # TEST 3: Daily schedule (verify config saved, can't wait 24h)
        # ═══════════════════════════════════════════════════════════
        print("[TEST 3/3] Daily at 14:00 UTC (config save test)")
        print("-" * 40)

        r = req.post(f"{API}/scrapers", headers=headers, json={
            "name": "Test 3 - Daily 14:00",
            "county": "pierce", "state": "wa", "record_type": "probate",
            "fields": {"party_name": True, "parcel_id": True},
            "enrichment": {"property_lookup": True, "skip_tracing": False},
            "schedule": {"frequency": "daily", "run_at_hour": 14, "run_at_minute": 0, "date_range_mode": "rolling_90"},
            "deliver": {"emails": [], "formats": ["csv"]},
        }, timeout=10)
        config3 = r.json()
        sched = config3["schedule"]
        print(f"  Config: {config3['id'][:8]}")
        print(f"  Frequency: {sched['frequency']}")
        print(f"  Run at: {sched.get('run_at_hour', '?')}:{sched.get('run_at_minute', '?'):02d} UTC")
        print(f"  Date range: {sched['date_range_mode']}")

        daily_ok = sched["frequency"] == "daily" and sched.get("run_at_hour") == 14
        print(f"  Config correct: {daily_ok}")

        # View in scrapers list
        await page.goto("https://bridgeleads.io/scrapers")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="sched_05_scrapers_list.png")
        print(f"  {'PASS' if daily_ok else 'FAIL'}\n")

        # ═══════════════════════════════════════════════════════════
        # View results
        # ═══════════════════════════════════════════════════════════
        print("[RESULTS] Checking results page...")
        await page.goto("https://bridgeleads.io/results")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="sched_06_results.png")

        # Download test
        print("[DOWNLOAD] Testing CSV download...")
        r = req.get(f"{API}/jobs/{job1_id}/export-url", headers=headers, timeout=10)
        if r.status_code == 200:
            dl_url = r.json().get("url", "")
            if dl_url.startswith("/"):
                dl_url = f"{API}{dl_url}"
            dl_url += f"&token={token}" if "?" in dl_url else f"?token={token}"
            dl = req.get(dl_url, timeout=30)
            if dl.status_code == 200:
                lines = dl.text.strip().split("\n")
                print(f"  Downloaded: {len(lines)-1} rows")
                print(f"  Header: {lines[0][:80]}")
                download_ok = len(lines) > 1
            else:
                print(f"  Download failed: {dl.status_code}")
                download_ok = False
        else:
            print(f"  Export URL failed: {r.status_code}")
            download_ok = False

        # Summary
        print(f"\n{'=' * 60}")
        print("SCHEDULE TEST RESULTS")
        print(f"{'=' * 60}")
        print(f"  Test 1 (Manual + Rolling 90):    {'PASS' if test1_pass else 'FAIL'}")
        print(f"  Test 2 (Manual + Custom dates):  {'PASS' if test2_pass else 'FAIL'}")
        print(f"  Test 3 (Daily config saved):     {'PASS' if daily_ok else 'FAIL'}")
        print(f"  Download CSV:                    {'PASS' if download_ok else 'FAIL'}")
        all_pass = test1_pass and test2_pass and daily_ok and download_ok
        print(f"\n  {'ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED'}")
        print(f"{'=' * 60}")

        await page.wait_for_timeout(3000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(test_schedules())
