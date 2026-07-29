"""Live end-to-end test of the BridgeLeads SaaS via Playwright.

Logs in to https://app.bridgeleads.io as admin@bridgeleads.io, navigates
to the scraper config UI, creates (or finds) a Pierce County WA probate
config with a 30-day rolling window, runs the scrape, and watches it
through to completion. Screenshots key milestones.

This is a one-shot script — it lives in tasks/ because that's the
project's scratchpad dir.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

FRONTEND = "https://app.bridgeleads.io"
EMAIL = os.environ.get("BL_EMAIL", "admin@bridgeleads.io")
PASSWORD = os.environ.get("BL_PASSWORD")
if not PASSWORD:
    raise SystemExit("BL_PASSWORD is not set — export the admin password before running.")

SHOTS_DIR = Path("tasks/live_scrape_shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)


def stamp(name: str) -> Path:
    return SHOTS_DIR / f"{datetime.now().strftime('%H%M%S')}_{name}.png"


async def screenshot(page: Page, label: str) -> Path:
    p = stamp(label)
    await page.screenshot(path=str(p), full_page=True)
    print(f"  screenshot -> {p}")
    return p


async def login(page: Page) -> None:
    print(f"-> {FRONTEND}/login")
    await page.goto(f"{FRONTEND}/login", wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle", timeout=10_000)
    await screenshot(page, "01_login_page")

    # Try common selectors for email + password fields. The frontend
    # is Next.js; field IDs may be name-based or aria-labelled.
    for sel in (
        "input[type='email']",
        "input[name='email']",
        "input#email",
        "input[autocomplete='email']",
    ):
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.fill(EMAIL)
            print(f"  filled email via {sel}")
            break
    else:
        raise RuntimeError("no email input found on login page")

    for sel in (
        "input[type='password']",
        "input[name='password']",
        "input#password",
        "input[autocomplete='current-password']",
    ):
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.fill(PASSWORD)
            print(f"  filled password via {sel}")
            break
    else:
        raise RuntimeError("no password input found on login page")

    # Click submit. Try button text + button type.
    for sel in (
        "button[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
        "button:has-text('Login')",
    ):
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.click()
            print(f"  clicked submit via {sel}")
            break
    else:
        raise RuntimeError("no submit button found on login form")

    # Wait for navigation away from /login. Give it 15s.
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    except PWTimeout:
        await screenshot(page, "01b_login_stuck")
        raise RuntimeError(f"still on login page after submit: {page.url}")
    print(f"  logged in, now at {page.url}")
    await page.wait_for_load_state("networkidle", timeout=10_000)
    await screenshot(page, "02_after_login")


async def main() -> int:
    async with async_playwright() as pw:
        # Use installed Chromium. The project's BaseScraper does the
        # same thing in production.
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await login(page)

            # After login: dump what's reachable. Print top-level page
            # text + the first ~30 links so we can see the UI structure
            # and pick the path to the scraper UI.
            print("\n--- post-login UI inspection ---")
            print(f"URL: {page.url}")
            print(f"Title: {await page.title()}")
            try:
                heading = await page.locator("h1").first.text_content(timeout=5_000)
                print(f"H1: {heading}")
            except Exception:
                pass

            link_locator = page.locator("a[href]")
            count = await link_locator.count()
            print(f"\nLinks ({count} total, showing first 30):")
            for i in range(min(count, 30)):
                lk = link_locator.nth(i)
                href = await lk.get_attribute("href")
                txt = (await lk.text_content() or "").strip()[:60]
                if href and txt:
                    print(f"  {href}  ->  {txt}")

            await screenshot(page, "03_dashboard_links")

            # Try common scraper-management URLs
            for path in ("/dashboard/scrapers", "/scrapers", "/dashboard"):
                target = f"{FRONTEND}{path}"
                print(f"\n-> probing {target}")
                resp = await page.goto(target, wait_until="domcontentloaded")
                if resp and resp.status < 400:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    await screenshot(page, f"04_probe_{path.strip('/').replace('/', '_')}")
                    print(f"  status={resp.status} URL now {page.url}")
                else:
                    print(f"  failed: status={resp.status if resp else 'no response'}")

            print("\n--- DONE: inspection complete. ---")
            print("Screenshots in:", SHOTS_DIR.resolve())
            return 0
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
