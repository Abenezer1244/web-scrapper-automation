"""End-to-end live scrape via the BridgeLeads UI.

Pierce County WA probate, 30-day rolling window. Logs in, configures a
scraper if one doesn't exist, runs it, watches the job to completion,
screenshots key milestones.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

FRONTEND = "https://app.bridgeleads.io"
EMAIL = os.environ.get("BL_EMAIL", "admin@bridgeleads.io")
PASSWORD = os.environ.get("BL_PASSWORD")
if not PASSWORD:
    raise SystemExit("BL_PASSWORD is not set — export the admin password before running.")

SHOTS = Path("tasks/live_scrape_shots")
SHOTS.mkdir(parents=True, exist_ok=True)


def stamp(name: str) -> Path:
    return SHOTS / f"{datetime.now().strftime('%H%M%S')}_{name}.png"


async def shot(page: Page, label: str) -> None:
    p = stamp(label)
    await page.screenshot(path=str(p), full_page=True)
    print(f"  shot -> {p}")


async def login(page: Page) -> None:
    await page.goto(f"{FRONTEND}/login", wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle", timeout=10_000)
    await page.locator("input[type='email']").first.fill(EMAIL)
    await page.locator("input[type='password']").first.fill(PASSWORD)
    await page.locator("button[type='submit']").first.click()
    await page.wait_for_url(lambda u: "/login" not in u, timeout=15_000)
    print(f"logged in, now at {page.url}")


async def go_new_scraper(page: Page) -> None:
    await page.goto(f"{FRONTEND}/scrapers/new", wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle", timeout=10_000)
    await shot(page, "10_new_scraper_form")
    print(f"on {page.url}, title={await page.title()}")

    # Dump every input + button on the page so we can see the form shape.
    print("\nForm inputs:")
    inputs = page.locator("input, select, textarea")
    for i in range(await inputs.count()):
        el = inputs.nth(i)
        tag = await el.evaluate("e => e.tagName")
        name = await el.get_attribute("name") or ""
        id_ = await el.get_attribute("id") or ""
        type_ = await el.get_attribute("type") or ""
        placeholder = await el.get_attribute("placeholder") or ""
        print(f"  {tag}  name={name!r:25} id={id_!r:25} type={type_!r:12} placeholder={placeholder!r}")

    print("\nButtons:")
    btns = page.locator("button, a[role='button']")
    for i in range(await btns.count()):
        b = btns.nth(i)
        txt = (await b.text_content() or "").strip()[:60]
        if txt:
            print(f"  {txt!r}")

    print("\nLabels (so we can see grouping):")
    labels = page.locator("label, h1, h2, h3, h4")
    for i in range(min(await labels.count(), 30)):
        txt = (await labels.nth(i).text_content() or "").strip()[:80]
        if txt:
            print(f"  {txt!r}")


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        try:
            await login(page)
            await go_new_scraper(page)
            return 0
        except Exception as exc:
            print(f"\nERROR: {exc}")
            await shot(page, "99_error")
            return 1
        finally:
            await ctx.close()
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
