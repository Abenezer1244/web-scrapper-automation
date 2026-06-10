"""Find ATIP owner/mailing endpoint."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["CAPTCHA_API_KEY"] = "af6f1a7bcff35945316c12c8ae15f829"
os.environ["CAPTCHA_ENABLED"] = "true"

from dotenv import load_dotenv

load_dotenv()

from src.scrapers.base_scraper import BridgeScraper
from src.scrapers.enrichment.captcha import solve_recaptcha

JS_CODE = """
async (args) => {
    const [token] = args;
    const h = {'Accept':'application/json','recaptcha-response':token};
    const out = [];

    const urls = [
        '/api/dynamicQueryOwnerLookup?value=5000190130',
        '/api/dynamicQueryOwnerLookup?parcelNumber=5000190130',
        '/api/geocoder?value=5000190130',
        '/api/reverseGeocoder?parcelNumber=5000190130',
        '/api/config?filter=' + encodeURIComponent('{"criteria":[{"like":["name","app.parcelSearch.detail%"]}]}'),
    ];

    for (const url of urls) {
        try {
            const r = await fetch(url, {headers: h});
            const ct = r.headers.get('content-type') || '';
            const body = await r.text();
            out.push({
                status: r.status,
                url: url.substring(0, 70),
                json: ct.includes('json'),
                hasMail: body.toLowerCase().includes('mail'),
                body: body.substring(0, 500)
            });
        } catch(e) {
            out.push({status: 'ERR', url: url.substring(0, 70), body: e.message});
        }
    }
    return out;
}
"""


async def main():
    token = await solve_recaptcha(
        "https://atip.piercecountywa.gov/app/parcelSearch",
        "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT",
    )
    if not token:
        print("CAPTCHA failed")
        return

    async with BridgeScraper() as s:
        await s.navigate("https://atip.piercecountywa.gov/app/parcelSearch")
        await s.page.wait_for_timeout(2_000)

        results = await s.page.evaluate(JS_CODE, [token])
        for r in results:
            flag = "MAIL" if r.get("hasMail") else ""
            print(f"  {r['status']} {flag:4s} {r.get('url', '?')}")
            if r.get("status") == 200:
                print(f"    {r.get('body', '')[:400]}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
