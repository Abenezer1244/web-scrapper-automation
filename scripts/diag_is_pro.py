"""Diagnostic: log in, get jobs, find 'is pro', inspect its records.

Relaxed load-state waits — some routes keep long-poll sockets open which
makes networkidle never fire.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from _creds import admin_creds

BASE = "https://app.bridgeleads.io"
EMAIL, PASSWORD = admin_creds()
OUT = Path(__file__).parent.parent / "_diag_is_pro"
OUT.mkdir(exist_ok=True)


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()

        print("[1] login")
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.fill('input[type="email"]', EMAIL)
        await page.fill('input[type="password"]', PASSWORD)
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            await page.click('button[type="submit"]')
        await asyncio.sleep(3)
        print(f"    after login URL: {page.url}")

        # Go to jobs/history
        print("[2] /jobs")
        await page.goto(f"{BASE}/jobs", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        await page.screenshot(path=str(OUT / "jobs.png"), full_page=True)
        (OUT / "jobs.html").write_text(await page.content(), encoding="utf-8")

        # dump visible text
        txt = await page.evaluate("() => document.body.innerText")
        (OUT / "jobs.txt").write_text(txt, encoding="utf-8")
        print(f"    jobs.txt bytes={len(txt)}")

        # Call jobs APIs
        for path in [
            "/api/jobs?limit=50",
            "/api/jobs",
            "/api/v1/jobs",
            "/api/scrapers/jobs",
        ]:
            try:
                resp = await page.evaluate(
                    f"""async () => {{
                        const r = await fetch('{path}', {{credentials:'include'}});
                        return {{ status: r.status, body: await r.text() }};
                    }}"""
                )
                fname = "api_" + path.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_") + ".json"
                (OUT / fname).write_text(json.dumps(resp, indent=2), encoding="utf-8")
                print(f"    {path} -> {resp.get('status')} bytes={len(resp.get('body',''))}")
            except Exception as e:
                print(f"    {path} err={e}")

        print(f"[DONE] {OUT}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
