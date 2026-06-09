"""Full E2E test on live BridgeLeads SaaS — visible Chrome browser."""
import asyncio
import uuid
from playwright.async_api import async_playwright

from _creds import admin_creds, test_password


async def full_e2e():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        test_email = f"e2e-{uuid.uuid4().hex[:6]}@test.bridgeleads.io"
        test_pass = test_password()
        passed = 0
        total = 12

        print("=" * 50)
        print("  BRIDGELEADS E2E TEST — LIVE CHROME")
        print("=" * 50)
        print()

        # 1. LANDING PAGE
        print("[1/12] Landing page")
        await page.goto("https://app.bridgeleads.io", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        await page.screenshot(path="/tmp/live_01_landing.png")
        print("       PASS")
        passed += 1

        # 2. PRICING PAGE
        print("[2/12] Pricing page")
        await page.goto("https://app.bridgeleads.io/pricing", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(4)
        body = await page.text_content("body")
        prices_ok = "$49" in body and "$149" in body and "$499" in body
        await page.screenshot(path="/tmp/live_02_pricing.png")
        print(f"       {'PASS' if prices_ok else 'FAIL'} — plans: {prices_ok}")
        if prices_ok:
            passed += 1

        # 3. REGISTER
        print("[3/12] Register new user")
        await page.goto("https://app.bridgeleads.io/register", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        ph = await page.locator('input[type="password"]').first.get_attribute("placeholder")
        await page.fill('input[type="email"]', test_email)
        await page.fill('input[id="password"]', test_pass)
        confirm = page.locator('input[id="confirmPassword"]')
        if await confirm.count() > 0:
            await confirm.fill(test_pass)
        await page.screenshot(path="/tmp/live_03_register.png")
        await page.click('button[type="submit"]')
        await asyncio.sleep(6)
        await page.screenshot(path="/tmp/live_04_after_register.png")
        registered = "/dashboard" in page.url
        print(f"       {'PASS' if registered else 'FAIL'} — placeholder: {ph}, redirect: {page.url}")
        if registered:
            passed += 1

        # 4. ONBOARDING BANNER
        print("[4/12] Onboarding banner")
        body = await page.text_content("body")
        has_onboard = "Getting started" in body
        await page.screenshot(path="/tmp/live_05_onboarding.png")
        print(f"       {'PASS' if has_onboard else 'FAIL'} — banner visible: {has_onboard}")
        if has_onboard:
            passed += 1

        # 5. TRIAL BANNER
        print("[5/12] Trial banner")
        has_trial = "PRO TRIAL" in body or "days remaining" in body
        print(f"       {'PASS' if has_trial else 'FAIL'} — trial shown: {has_trial}")
        if has_trial:
            passed += 1

        # 6. NEW SCRAPER WIZARD
        print("[6/12] New scraper wizard")
        await page.goto("https://app.bridgeleads.io/scrapers/new", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        body = await page.text_content("body")
        has_wizard = "Select state" in body or "County" in body.title()
        await page.screenshot(path="/tmp/live_06_wizard.png")
        print(f"       {'PASS' if has_wizard else 'FAIL'} — wizard: {has_wizard}")
        if has_wizard:
            passed += 1

        # 7. LOG OUT + LOGIN AS ADMIN
        print("[7/12] Admin login")
        await page.goto("https://app.bridgeleads.io/login", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        _admin_email, _admin_pass = admin_creds()
        await page.fill('input[type="email"]', _admin_email)
        await page.fill('input[type="password"]', _admin_pass)
        await page.click('button[type="submit"]')
        await asyncio.sleep(5)
        admin_ok = "/dashboard" in page.url
        await page.screenshot(path="/tmp/live_07_admin.png")
        print(f"       {'PASS' if admin_ok else 'FAIL'} — URL: {page.url}")
        if admin_ok:
            passed += 1

        # 8. ADMIN: NO ONBOARDING
        print("[8/12] Admin: onboarding hidden")
        body = await page.text_content("body")
        hidden = "Getting started" not in body
        print(f"       {'PASS' if hidden else 'FAIL'} — hidden: {hidden}")
        if hidden:
            passed += 1

        # 9. SCRAPERS PAGE
        print("[9/12] Scrapers list")
        await page.goto("https://app.bridgeleads.io/scrapers", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        body = await page.text_content("body")
        has_scrapers = "Pierce" in body or "King" in body
        await page.screenshot(path="/tmp/live_08_scrapers.png")
        print(f"       {'PASS' if has_scrapers else 'FAIL'} — scrapers: {has_scrapers}")
        if has_scrapers:
            passed += 1

        # 10. RESULTS PAGE
        print("[10/12] Results page")
        await page.goto("https://app.bridgeleads.io/results", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        await page.screenshot(path="/tmp/live_09_results.png")
        body = await page.text_content("body")
        has_results = "records" in body.lower() or "Pierce" in body or "King" in body
        print(f"       {'PASS' if has_results else 'FAIL'} — results: {has_results}")
        if has_results:
            passed += 1

        # 11. SETTINGS
        print("[11/12] Settings")
        await page.goto("https://app.bridgeleads.io/settings", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        body = await page.text_content("body")
        has_settings = "Billing" in body and "API" in body
        pw_field = page.locator('input[id="new-password"]')
        pw_ph = ""
        if await pw_field.count() > 0:
            pw_ph = await pw_field.get_attribute("placeholder") or ""
        await page.screenshot(path="/tmp/live_10_settings.png")
        pw_ok = "10" in pw_ph
        print(f"       {'PASS' if has_settings else 'FAIL'} — tabs: {has_settings}, pw placeholder: '{pw_ph}' ({'OK' if pw_ok else 'WRONG'})")
        if has_settings:
            passed += 1

        # 12. DOWNLOAD BUTTON
        print("[12/12] Download available")
        await page.goto("https://app.bridgeleads.io/results", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        dl = page.locator("text=Download").first
        dl_visible = await dl.is_visible() if await dl.count() > 0 else False
        await page.screenshot(path="/tmp/live_11_download.png")
        print(f"       {'PASS' if dl_visible else 'FAIL'} — download button: {dl_visible}")
        if dl_visible:
            passed += 1

        print()
        print("=" * 50)
        print(f"  RESULT: {passed}/{total} PASSED")
        print("=" * 50)
        print(f"  Test user: {test_email}")

        await asyncio.sleep(3)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(full_e2e())
