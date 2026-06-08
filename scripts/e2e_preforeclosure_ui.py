"""E2E: Open app.bridgeleads.io, login, run King County pre-foreclosure scrape via UI.

Run: PLAYWRIGHT_HEADLESS=false python scripts/e2e_preforeclosure_ui.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false")

from _creds import fixture_creds


def safe_print(msg):
    print(msg.encode('ascii', 'replace').decode())


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=300)
        page = await browser.new_page()

        # 1. Landing page
        print("Opening app.bridgeleads.io...")
        await page.goto("https://app.bridgeleads.io", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # 2. Click Login
        login_link = page.locator('a:has-text("Log in"), a[href*="login"]')
        if await login_link.count() > 0:
            await login_link.first.click()
            await page.wait_for_timeout(3000)

        # 3. Fill login
        _fix_email, _fix_pass = fixture_creds()
        await page.locator('input[type="email"], input[name="email"]').first.fill(_fix_email)
        await page.locator('input[type="password"]').first.fill(_fix_pass)
        await page.locator('button[type="submit"]').first.click()
        await page.wait_for_timeout(5000)
        print(f"Logged in: {page.url}")
        await page.screenshot(path="e2e_01_dashboard.png")

        # 4. Check if King County Pre-Foreclosure config already exists
        body = (await page.inner_text('body')).encode('ascii', 'replace').decode()

        if "pre-foreclosure" in body.lower() or "pre_foreclosure" in body.lower():
            print("Found existing pre-foreclosure config on dashboard")
            # Click Run on it
            run_btns = page.locator('button:has-text("Run"), button:has-text("Scrape")')
            if await run_btns.count() > 0:
                print(f"Found {await run_btns.count()} Run buttons")
        else:
            print("No pre-foreclosure config found — navigating to create one")

        # 5. Try to navigate to new scrape page
        new_btn = page.locator('a:has-text("New"), button:has-text("New"), a[href*="new"], a[href*="wizard"], a[href*="create"]')
        if await new_btn.count() > 0:
            await new_btn.first.click()
            await page.wait_for_timeout(3000)
            print(f"New scrape page: {page.url}")
            await page.screenshot(path="e2e_02_new_scrape.png")

        # 6. Select King County
        king_option = page.locator('text=King, [value="king"], option:has-text("King")')
        county_select = page.locator('select[name*="county"], [data-testid*="county"]')

        if await county_select.count() > 0:
            await county_select.first.select_option(label="King")
            print("Selected King County")
            await page.wait_for_timeout(1000)

        # 7. Select Pre-Foreclosure record type
        preforeclosure = page.locator('text=Pre-Foreclosure, text=pre_foreclosure, [value="pre_foreclosure"]')
        record_select = page.locator('select[name*="record"], [data-testid*="record"]')

        if await record_select.count() > 0:
            await record_select.first.select_option(value="pre_foreclosure")
            print("Selected Pre-Foreclosure")
            await page.wait_for_timeout(1000)

        await page.screenshot(path="e2e_03_configured.png")

        # 8. Submit / Run
        submit = page.locator('button:has-text("Run"), button:has-text("Start"), button:has-text("Create"), button[type="submit"]')
        if await submit.count() > 0:
            await submit.first.click()
            print("Clicked Run/Submit")
            await page.wait_for_timeout(5000)
            await page.screenshot(path="e2e_04_running.png")

        # 9. Wait and watch the job progress
        print("\n=== Job running — monitoring for 5 minutes ===\n")
        for i in range(30):
            await page.wait_for_timeout(10000)
            await page.screenshot(path=f"e2e_progress_{i:02d}.png")
            body = (await page.inner_text('body')).encode('ascii', 'replace').decode()
            # Look for status indicators
            for keyword in ["complete", "done", "records", "failed", "error"]:
                if keyword in body.lower():
                    safe_print(f"[{(i+1)*10}s] Found '{keyword}' on page")
                    break

            if "complete" in body.lower() or "done" in body.lower():
                print("Job appears complete!")
                await page.screenshot(path="e2e_05_complete.png")
                break

        await page.screenshot(path="e2e_final.png")
        print("\nFinal screenshot saved. Browser closing.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
