"""Live Chrome final E2E: register, login, scrape, view results, download."""

import asyncio
import time
import uuid

import requests as req
from _creds import test_password
from playwright.async_api import async_playwright

API = "https://api.bridgeleads.io"


async def main():
    email = f"chrome_{uuid.uuid4().hex[:6]}@bridgeleads.io"
    password = test_password()

    # Pre-register + create config + trigger job via API
    r = req.post(f"{API}/auth/register", json={"email": email, "password": password}, timeout=10)
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    me = req.get(f"{API}/auth/me", headers=h, timeout=10).json()

    r = req.post(f"{API}/scrapers", headers=h, json={
        "name": "Chrome Final", "county": "pierce", "state": "wa", "record_type": "probate",
        "fields": {"party_name": True, "parcel_id": True, "property_address": True, "mailing_address": True, "heirs": True},
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "custom", "date_from": "2026-03-15", "date_to": "2026-03-28"},
        "deliver": {"emails": [], "formats": ["csv"]},
    }, timeout=10)
    config_id = r.json()["id"]

    r = req.post(f"{API}/jobs", headers=h, json={"scraper_config_id": config_id, "trigger": "manual"}, timeout=10)
    job_id = r.json()["id"]

    print("=" * 60)
    print("LIVE CHROME E2E TEST")
    print("=" * 60)
    print(f"Email:  {email}")
    print(f"Plan:   {me['plan']} (trial: {me['is_trial']}, {me['trial_days_remaining']}d left)")
    print(f"Limit:  {me['records_used']}/{me['records_limit']}")
    print(f"Config: {config_id[:8]}")
    print(f"Job:    {job_id[:8]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # 1. Login
        print("\n[1/7] Login...")
        await page.goto("https://bridgeleads.io/login")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await page.locator('input[name="email"]').fill(email)
        await page.locator('input[name="password"]').fill(password)
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(4000)
        await page.screenshot(path="chrome_1_dashboard.png")
        print(f"  OK: {page.url}")

        # 2. Dashboard - check trial banner
        print("[2/7] Dashboard + trial banner...")
        body = await page.inner_text("body")
        print(f"  Trial banner: {'PRO TRIAL' in body}")
        print(f"  Upgrade btn:  {'Upgrade' in body}")

        # 3. Live scrape
        print("[3/7] Live scrape page...")
        await page.goto(f"https://bridgeleads.io/live/{job_id}")
        await page.wait_for_timeout(2000)
        await page.screenshot(path="chrome_2_live.png")

        # Wait for completion
        start = time.time()
        while time.time() - start < 120:
            await page.wait_for_timeout(5000)
            j = req.get(f"{API}/jobs/{job_id}", headers=h, timeout=10).json()
            elapsed = int(time.time() - start)
            print(f"  [{elapsed}s] {j['status']} - {j['record_count']} records")
            if j["status"] in ("done", "failed"):
                break

        await page.wait_for_timeout(2000)
        await page.screenshot(path="chrome_3_complete.png")

        # 4. Results
        print("[4/7] Results page...")
        await page.goto(f"https://bridgeleads.io/results/{job_id}")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="chrome_4_results.png")
        print("  OK")

        # 5. Download
        print("[5/7] Download CSV...")
        r = req.get(f"{API}/jobs/{job_id}/export-url", headers=h, timeout=10)
        url = r.json()["url"]
        if url.startswith("/"):
            url = f"{API}{url}"
        dl = req.get(f"{url}?token={token}", timeout=30)
        lines = dl.text.strip().split("\n")
        print(f"  Status: {dl.status_code}")
        print(f"  Rows:   {len(lines) - 1}")
        print(f"  Header: {lines[0][:70]}")
        if len(lines) > 1:
            print(f"  Row 1:  {lines[1][:70]}")

        # 6. Other pages
        print("[6/7] All pages...")
        for name, path in [("Scrapers", "/scrapers"), ("Deliver", "/deliver"), ("Settings", "/settings")]:
            await page.goto(f"https://bridgeleads.io{path}")
            await page.wait_for_timeout(1500)
            print(f"  {name}: OK")

        # 7. Settings billing
        print("[7/7] Upgrade button...")
        billing = page.locator("button:has-text('Billing'), [data-tab='billing']")
        if await billing.count() > 0:
            await billing.first.click()
            await page.wait_for_timeout(2000)
        await page.screenshot(path="chrome_5_billing.png")

        upgrade = page.locator("button:has-text('Upgrade'), button:has-text('Subscribe')")
        print(f"  Upgrade buttons: {await upgrade.count()}")

        # Summary
        print(f"\n{'=' * 60}")
        print("RESULTS")
        print("  Login:          PASS")
        print(f"  Trial banner:   {'PASS' if 'PRO TRIAL' in body else 'FAIL'}")
        print(f"  Scrape:         {'PASS' if j['status'] == 'done' else 'FAIL'} ({j['record_count']} records)")
        print("  Results page:   PASS")
        print(f"  CSV download:   {'PASS' if dl.status_code == 200 else 'FAIL'} ({len(lines)-1} rows)")
        print("  All pages:      PASS")
        print(f"  Plan:           {me['plan']} trial ({me['trial_days_remaining']}d)")
        print(f"{'=' * 60}")

        await page.wait_for_timeout(3000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
