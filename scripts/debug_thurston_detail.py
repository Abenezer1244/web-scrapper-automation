"""Fetch one Thurston detail page through the same code path the scraper uses,
and dump what comes back so we can see why parcel extraction returns 0.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    import requests

    from src.scrapers.templates.eagleweb import EagleWebScraper

    async with EagleWebScraper(
        base_url="https://eagleweb.co.thurston.wa.us/thurstonrecorder/web/",
        county="Thurston",
        state="WA",
        record_types=["probate"],
    ) as scraper:
        # Accept disclaimer + configure search form
        await scraper.navigate(scraper.base_url)
        await scraper._accept_disclaimer()
        await scraper._configure_search("probate", "03/01/2026", "04/01/2026")
        await scraper._submit_search()

        # Grab one detail_href from the first results page
        raw = await scraper.page.evaluate("""
            (() => {
                const links = document.querySelectorAll('a[href*="docDetail"], a[href*="Detail"], a[href*="viewDoc"]');
                return Array.from(links).slice(0, 3).map(a => a.href);
            })()
        """)

        print(f"Found {len(raw)} detail links on page 1", flush=True)
        for h in raw:
            print(f"  {h}", flush=True)

        if not raw:
            return 1

        # Copy browser cookies (same approach as _fetch_parcel_ids_from_details)
        cookies = await scraper.page.context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        print(f"\nBrowser cookies: {list(cookie_dict.keys())}", flush=True)

        # Fetch via HTTP
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
        url = raw[0]
        print(f"\nFetching: {url}", flush=True)
        resp = requests.get(url, headers=headers, cookies=cookie_dict, timeout=10)
        print(f"Status: {resp.status_code}", flush=True)
        print(f"Body length: {len(resp.text)}", flush=True)

        # Look for parcel markers
        text = resp.text
        for marker in ["Parcel", "parcel", "Tax ID", "APN", "TaxID", "Tax Parcel"]:
            idx = text.find(marker)
            if idx >= 0:
                snippet = text[idx:idx + 300].replace("\n", " ").replace("  ", " ")
                print(f"\nFound '{marker}' at offset {idx}:", flush=True)
                print(f"  {snippet[:280]}", flush=True)

        # Also fetch via browser page.goto (bypasses cookie issue) to compare
        print("\n--- Fetching same URL via Playwright page.goto ---", flush=True)
        await scraper.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        pw_html = await scraper.page.content()
        print(f"Playwright HTML length: {len(pw_html)}", flush=True)
        for marker in ["Parcel", "parcel", "APN"]:
            idx = pw_html.find(marker)
            if idx >= 0:
                snippet = pw_html[idx:idx + 300].replace("\n", " ").replace("  ", " ")
                print(f"[PW] Found '{marker}' at offset {idx}: {snippet[:280]}", flush=True)
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
