"""Full E2E test: every page on app.bridgeleads.io with timing and error capture."""

import asyncio
import re
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false")

from playwright.async_api import async_playwright

from _creds import fixture_creds


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome", slow_mo=200)
        page = await browser.new_page()

        errors = []
        page.on("pageerror", lambda err: errors.append(f"JS_CRASH: {err.message[:150]}"))
        page.on("console", lambda msg: errors.append(f"CONSOLE_ERR: {msg.text[:150]}") if msg.type == "error" else None)

        timings = {}

        # 1. Landing
        t0 = time.time()
        await page.goto("https://app.bridgeleads.io", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        timings["landing"] = round(time.time() - t0, 1)
        print(f"1. Landing: {timings['landing']}s | errors={len(errors)}")
        await page.screenshot(path="e2e_full_01_landing.png")

        # 2. Login
        errors.clear()
        t0 = time.time()
        login = page.locator('a[href*="login"]')
        if await login.count() > 0:
            await login.first.click()
            await page.wait_for_timeout(2000)
        _fix_email, _fix_pass = fixture_creds()
        await page.locator('input[type="email"]').first.fill(_fix_email)
        await page.locator('input[type="password"]').first.fill(_fix_pass)
        await page.locator('button[type="submit"]').first.click()
        await page.wait_for_timeout(4000)
        timings["login"] = round(time.time() - t0, 1)
        print(f"2. Login: {timings['login']}s -> {page.url} | errors={len(errors)}")
        for e in errors[:2]:
            print(f"   {e}")
        await page.screenshot(path="e2e_full_02_dashboard.png")

        # 3-8. Page navigation tests
        pages_to_test = [
            ("dashboard", "/dashboard"),
            ("scrapers", "/scrapers"),
            ("wizard", "/scrapers/new"),
            ("results", "/results"),
            ("settings", "/settings"),
            ("counties", "/counties"),
        ]

        for i, (name, path) in enumerate(pages_to_test, 3):
            errors.clear()
            t0 = time.time()
            try:
                await page.goto(f"https://app.bridgeleads.io{path}", wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
            except Exception as ex:
                print(f"{i}. {name}: TIMEOUT ({str(ex)[:50]})")
                continue
            timings[name] = round(time.time() - t0, 1)
            flag = " *** SLOW" if timings[name] > 5 else ""
            print(f"{i}. {name}: {timings[name]}s | errors={len(errors)}{flag}")
            for e in errors[:2]:
                print(f"   {e}")
            await page.screenshot(path=f"e2e_full_{i:02d}_{name}.png")

        # 9. Run a scrape
        errors.clear()
        print("\n--- Running Pre-Foreclosure scrape ---")
        await page.goto("https://app.bridgeleads.io/scrapers", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # Click on config
        config_link = page.locator("a, tr, div").filter(has_text=re.compile(r"pre.foreclosure|Pre.Foreclosure", re.IGNORECASE)).first
        if await config_link.count() > 0:
            await config_link.click()
            await page.wait_for_timeout(2000)

            run_btn = page.locator('button:has-text("Run"), button:has-text("Scrape"), button:has-text("Start")')
            if await run_btn.count() > 0:
                t0 = time.time()
                await run_btn.first.click()
                await page.wait_for_timeout(5000)
                print(f"9. Run clicked -> {page.url}")
                await page.screenshot(path="e2e_full_09_live_run.png")

                # Monitor
                for tick in range(36):  # 6 minutes max
                    await page.wait_for_timeout(10000)
                    body = (await page.inner_text("body")).encode("ascii", "replace").decode()
                    m = re.search(r"(\d+)\s*record", body, re.IGNORECASE)
                    recs = m.group(1) if m else "0"
                    status = "done" if "complete" in body.lower() else "enriching" if "enriching" in body.lower() else "scraping" if "scraping" in body.lower() else "waiting"
                    elapsed = round(time.time() - t0)
                    print(f"   [{elapsed}s] {status} | {recs} records")

                    if status == "done":
                        timings["scrape_total"] = elapsed
                        await page.screenshot(path="e2e_full_10_complete.png")
                        break
            else:
                print("9. No Run button")
        else:
            print("9. No Pre-Foreclosure config found")

        # Summary
        print(f"\n{'='*50}")
        print("E2E TIMING SUMMARY")
        print(f"{'='*50}")
        for name, t in timings.items():
            flag = " ** SLOW" if t > 5 else ""
            print(f"  {name:15s}: {t}s{flag}")

        print("\nBrowser open for 60s...")
        await page.wait_for_timeout(60000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
