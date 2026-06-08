"""Run a Chelan probate scrape through the live SaaS."""
import asyncio
import os
import sys
from playwright.async_api import async_playwright

APP = "https://app.bridgeleads.io"
ADMIN_EMAIL = os.environ.get("BRIDGELEADS_ADMIN_EMAIL", "admin@bridgeleads.io")
ADMIN_PASSWORD = os.environ.get("BRIDGELEADS_ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    sys.exit("BRIDGELEADS_ADMIN_PASSWORD is not set — export it before running live_qa.")
PICS = "C:/Users/Windows/OneDrive - Seattle Colleges/Pictures/Saved Pictures"


async def shot(page, name):
    try:
        await page.screenshot(path=f"{PICS}/{name}.png", timeout=10000)
        print(f"   Screenshot: {name}.png")
    except Exception:
        print(f"   (screenshot {name} timed out)")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # 1. Login
        print("1. Logging in...")
        await page.goto(f"{APP}/login")
        await page.wait_for_timeout(2000)
        if "dashboard" not in page.url and "scrapers" not in page.url:
            # Auto-fill login
            email_input = page.locator('input[type="email"], input[name="email"]').first
            pass_input = page.locator('input[type="password"], input[name="password"]').first
            if await email_input.count() > 0 and await pass_input.count() > 0:
                await email_input.fill(ADMIN_EMAIL)
                await pass_input.fill(ADMIN_PASSWORD)
                submit = page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")').first
                await submit.click()
                await page.wait_for_timeout(5000)
                print("   Auto-login submitted")
            if "dashboard" not in page.url:
                print("   Waiting for redirect...")
                try:
                    await page.wait_for_url("**/dashboard**", timeout=30_000)
                except Exception:
                    pass
        print(f"   OK! {page.url}")

        # 2. New scraper page
        print("2. New scraper -> Chelan Probate...")
        await page.goto(f"{APP}/scrapers/new")
        await page.wait_for_timeout(3000)

        # Click the state dropdown and select WA
        dropdown = page.locator("select").first
        if await dropdown.count() > 0:
            # Try value first, then label
            try:
                await dropdown.select_option(value="WA", timeout=5000)
                print("   Selected WA (by value)")
            except Exception:
                try:
                    await dropdown.select_option(label="WA", timeout=5000)
                    print("   Selected WA (by label)")
                except Exception:
                    # Click to open, then pick manually
                    await dropdown.click()
                    await page.wait_for_timeout(500)
                    await page.locator("option:has-text('WA')").first.click()
                    print("   Selected WA (by click)")
            await page.wait_for_timeout(2000)

        await shot(page, "chelan_01_state")

        # Pick Chelan county
        chelan = page.locator('text="Chelan"').first
        if await chelan.count() > 0:
            await chelan.click()
            await page.wait_for_timeout(1000)
            print("   Selected Chelan")
        else:
            # Maybe county is also a select
            county_sel = page.locator("select").nth(1)
            if await county_sel.count() > 0:
                await county_sel.select_option(label="Chelan")
                await page.wait_for_timeout(1000)
                print("   Selected Chelan from dropdown")

        await shot(page, "chelan_02_county")

        # Pick Probate
        probate = page.locator('text="Probate"').first
        if await probate.count() > 0:
            await probate.click()
            await page.wait_for_timeout(1000)
            print("   Selected Probate")
        await shot(page, "chelan_03_record_type")

        # Advance through steps
        for i in range(5):
            btn = page.locator('button:has-text("Continue"), button:has-text("Next")').first
            if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                await page.wait_for_timeout(2000)
                print(f"   Advanced step {i+1}")
                await shot(page, f"chelan_04_step{i+1}")

        # Check for Rolling 30 on schedule page
        body = await page.text_content("body") or ""
        print(f'   "Rolling 30" visible: {"Rolling 30" in body}')
        print(f'   "max 30 days" warning: {"maximum of 30" in body}')

        # Submit — look for Run/Create/Start button
        submit = page.locator('button:has-text("Run"), button:has-text("Create"), button:has-text("Start Scrape"), button[type="submit"]').first
        if await submit.count() > 0 and await submit.is_visible():
            print("3. Submitting scrape...")
            await submit.click()
            await page.wait_for_timeout(5000)
            await shot(page, "chelan_05_submitted")
            print(f"   Submitted! URL: {page.url}")

            # Wait for job to complete
            print("4. Waiting for completion (up to 12 min)...")
            for i in range(72):  # 72 x 10s = 12 min
                await page.wait_for_timeout(10000)
                txt = await page.text_content("body") or ""
                if "Complete" in txt or "Job complete" in txt:
                    print(f"   COMPLETE after ~{(i+1)*10}s!")
                    break
                if "Failed" in txt or "Error" in txt or "timed out" in txt.lower():
                    print(f"   FAILED after ~{(i+1)*10}s")
                    break
                if i % 6 == 0:
                    print(f"   Waiting... {(i+1)*10}s")
                    await shot(page, f"chelan_06_progress_{i}")

            await shot(page, "chelan_07_final")

            # Click to view results if there's a link
            results_link = page.locator('a:has-text("View Results"), a:has-text("results")').first
            if await results_link.count() > 0:
                await results_link.click()
                await page.wait_for_timeout(3000)

            await shot(page, "chelan_08_results")
            final_body = await page.text_content("body") or ""
            print(f'   Trust signal: {"Sourced from public" in final_body}')
            print(f'   Has records: {"records" in final_body.lower()}')
        else:
            print("   No submit button found — may need more steps")
            await shot(page, "chelan_05_stuck")

        print("\n=== DONE ===")
        print("Browser stays open 30s...")
        await page.wait_for_timeout(30000)
        await browser.close()


asyncio.run(main())
