"""
End-to-end scrape test: Register -> Create scraper -> Trigger job -> Monitor live view.
Uses API for setup, Playwright browser to monitor the live run.
"""
import asyncio
import uuid
from pathlib import Path

import requests
from playwright.async_api import async_playwright

API     = "https://api.bridgeleads.io"
APP     = "https://app.bridgeleads.io"
EMAIL   = f"scrapetest_{uuid.uuid4().hex[:8]}@bridgeleads.io"
PASSWORD = "TestPass123!"
SHOTS   = Path("tests/screenshots/scrape")


def log(msg):
    print(f"  >> {msg}")


async def run():
    SHOTS.mkdir(parents=True, exist_ok=True)

    # ── 1. Register via API ────────────────────────────────────────────────
    log(f"Registering {EMAIL}")
    r = requests.post(f"{API}/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 201, f"Register failed: {r.status_code} {r.text[:200]}"
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    log("Registered OK")

    # ── 2. Login via API ───────────────────────────────────────────────────
    log("Logging in")
    r = requests.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    log("Login OK")

    # ── 3. Get profile ─────────────────────────────────────────────────────
    r = requests.get(f"{API}/auth/me", headers=headers)
    assert r.status_code == 200, f"Profile failed: {r.status_code} {r.text[:200]}"
    me = r.json()
    log(f"Profile: plan={me['plan']} records={me['records_used']}/{me['records_limit']}")

    # ── 4. Create scraper config ───────────────────────────────────────────
    log("Creating scraper config: Pierce County, WA — Probate")
    r = requests.post(f"{API}/scrapers", json={
        "name": "Pierce County Probate - E2E Test",
        "county": "pierce",
        "state": "WA",
        "record_type": "probate",
        "schedule": {"frequency": "manual"},
        "deliver": {"emails": [EMAIL], "format": "csv"},
    }, headers=headers)
    assert r.status_code == 201, f"Create scraper failed: {r.status_code} {r.text[:200]}"
    config_id = r.json()["id"]
    log(f"Scraper config created: id={config_id}")

    # ── 5. Create job (trigger scrape) ─────────────────────────────────────
    log("Triggering scrape job...")
    r = requests.post(f"{API}/jobs", json={
        "scraper_config_id": config_id,
        "trigger": "manual",
    }, headers=headers)
    assert r.status_code == 201, f"Create job failed: {r.status_code} {r.text[:200]}"
    job_id = r.json()["id"]
    job_status = r.json()["status"]
    log(f"Job created: id={job_id} status={job_status}")

    # ── 6. Open browser and navigate to live view ──────────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # Login via browser (NextAuth)
        log("Browser: logging in via frontend")
        await page.goto(f"{APP}/login")
        await page.wait_for_load_state("networkidle")
        await page.fill("#email", EMAIL)
        await page.fill("#password", PASSWORD)
        await page.click('button[type="submit"]')

        try:
            await page.wait_for_url(f"{APP}/", timeout=15_000)
            log("Browser: logged in, on dashboard")
        except Exception:
            log(f"Browser: login redirect failed, url={page.url}")

        await page.screenshot(path=str(SHOTS / "01_dashboard.png"))

        # Navigate to live view (use "load" not "networkidle" — SSE keeps network active)
        log(f"Browser: opening live view for job {job_id}")
        await page.goto(f"{APP}/live/{job_id}")
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(3_000)  # let React hydrate
        await page.screenshot(path=str(SHOTS / "02_live_start.png"))

        # ── 7. Poll job status via API + capture browser screenshots ───────
        log("Monitoring job (polling every 15s, max 20 minutes)...")
        terminal_statuses = {"done", "failed", "cancelled"}
        shot_num = 3
        final_status = "unknown"

        for i in range(80):  # up to ~20 minutes
            await asyncio.sleep(15)

            # Check job status via API
            r = requests.get(f"{API}/jobs/{job_id}", headers=headers)
            if r.status_code == 200:
                job = r.json()
                final_status = job.get("status", "unknown")
                record_count = job.get("record_count", 0)
                page_current = job.get("page_current", 0)
                page_total = job.get("page_total", 0)
                error_msg = job.get("error_message", "")
                elapsed = i * 15
                log(f"[{elapsed}s] status={final_status} pages={page_current}/{page_total} records={record_count}" +
                    (f" error={error_msg[:80]}" if error_msg else ""))
            else:
                log(f"[{i*15}s] API check failed: {r.status_code}")

            # Capture browser screenshot
            await page.reload()
            await page.wait_for_load_state("load")
            await page.screenshot(path=str(SHOTS / f"{shot_num:02d}_live_{i*15}s.png"))
            shot_num += 1

            if final_status in terminal_statuses:
                log(f"Job reached terminal state: {final_status}")
                break

        # ── 8. Final state ─────────────────────────────────────────────────
        await page.screenshot(path=str(SHOTS / f"{shot_num:02d}_final.png"))

        # Check results if job succeeded
        if final_status == "done":
            log("SUCCESS - Job completed!")
            r = requests.get(f"{API}/jobs/{job_id}/results?page=1&page_size=5", headers=headers)
            if r.status_code == 200:
                results = r.json()
                log(f"Results: {len(results.get('items', []))} records on first page")
                for item in results.get("items", [])[:3]:
                    log(f"  - {item.get('party_name', 'N/A')} | {item.get('date_recorded', 'N/A')} | parcel={item.get('parcel_id', 'N/A')}")
            else:
                log(f"Results fetch: {r.status_code} {r.text[:200]}")

            # Check if export key is set
            r = requests.get(f"{API}/jobs/{job_id}", headers=headers)
            if r.status_code == 200:
                export_key = r.json().get("export_key")
                if export_key:
                    log(f"Export key: {export_key[:60]}...")
                else:
                    log("No export key yet (export may still be processing)")

            # Navigate to results page
            await page.goto(f"{APP}/results/{job_id}")
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(SHOTS / f"{shot_num + 1:02d}_results.png"))
            log("Results page screenshot captured")

        elif final_status == "failed":
            r = requests.get(f"{API}/jobs/{job_id}", headers=headers)
            error = r.json().get("error_message", "unknown") if r.status_code == 200 else "API error"
            log(f"FAILED - {error}")

        else:
            log(f"Job did not reach terminal state in time. Last status: {final_status}")

        await browser.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  Email:      {EMAIL}")
    print(f"  Job ID:     {job_id}")
    print(f"  Status:     {final_status}")
    print(f"  Screenshots: {SHOTS.resolve()}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(run())
