"""Probe Whatcom Helion recording portal — accept disclaimer, find search form."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        await page.goto("https://recording.whatcomcounty.us/", wait_until="domcontentloaded", timeout=30_000)
        print(f"Start URL: {page.url}", flush=True)
        title = await page.title()
        print(f"Title: {title}", flush=True)

        # Find + click disclaimer accept button
        buttons = await page.locator("button, input[type='submit']").all()
        print(f"\nButtons on disclaimer page ({len(buttons)}):", flush=True)
        for b in buttons[:10]:
            text = (await b.text_content() or "").strip() or (await b.get_attribute("value") or "")
            print(f"  {text!r}", flush=True)

        # Try common accept labels
        for label in ["Accept", "Agree", "I Accept", "I Agree", "Acknowledge", "Enter", "Continue"]:
            btn = page.locator(f"button:has-text('{label}'), input[type='submit'][value*='{label}' i]")
            if await btn.count() > 0:
                print(f"\nClicking '{label}' ...", flush=True)
                try:
                    async with page.expect_navigation(timeout=10_000):
                        await btn.first.click()
                except Exception:
                    pass
                break

        await page.wait_for_timeout(2000)
        print(f"\nAfter disclaimer URL: {page.url}", flush=True)
        print(f"Title: {await page.title()}", flush=True)

        # Jump to Advanced Search
        await page.goto("https://recording.whatcomcounty.us/?mode=Advanced", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)
        print(f"\nAdvanced search URL: {page.url}", flush=True)

        # Find form inputs on search page
        inputs = await page.locator("input, select, textarea").all()
        print(f"\nForm elements ({len(inputs)}):", flush=True)
        for inp in inputs[:25]:
            name = await inp.get_attribute("name") or ""
            id_ = await inp.get_attribute("id") or ""
            type_ = await inp.get_attribute("type") or ""
            placeholder = await inp.get_attribute("placeholder") or ""
            print(f"  id={id_!r:30s} name={name!r:25s} type={type_!r:10s} ph={placeholder!r}", flush=True)

        # Page link text (navigation)
        links = await page.locator("a").all()
        print(f"\nNav links (first 15):", flush=True)
        for a in links[:15]:
            text = (await a.text_content() or "").strip()
            href = (await a.get_attribute("href") or "")[:80]
            if text:
                print(f"  {text[:40]!r} -> {href}", flush=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
