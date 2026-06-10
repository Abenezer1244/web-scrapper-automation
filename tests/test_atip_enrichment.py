"""Test ATIP enrichment with CAPTCHA solving."""
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

JS_FETCH = """
async (args) => {
    const [url, token] = args;
    try {
        const r = await fetch(url, {
            headers: {
                'Accept': 'application/json',
                'recaptcha-response': token
            }
        });
        const body = await r.text();
        return {status: r.status, body: body.substring(0, 500)};
    } catch(e) {
        return {error: e.message};
    }
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

    print(f"Token obtained: {token[:30]}...")

    async with BridgeScraper() as s:
        await s.navigate("https://atip.piercecountywa.gov/app/parcelSearch")
        await s.page.wait_for_timeout(3_000)

        parcel = "5000190130"

        result = await s.page.evaluate("""
            async (args) => {
                const [parcel, token] = args;
                const results = [];
                const headers = {'Accept':'application/json','recaptcha-response':token};

                const urls = [
                    '/api/parcelSearch?value=' + parcel,
                    '/api/parcelSearch?value=' + parcel + '&searchType=parcel',
                    '/api/parcelSearch?searchValue=' + parcel,
                    '/api/parcelSearch/' + parcel,
                    '/api/parcelSearch?q=' + parcel,
                    '/api/parcelSearch?parcelNumber=' + parcel,
                ];

                for (const url of urls) {
                    try {
                        const r = await fetch(url, {headers});
                        const ct = r.headers.get('content-type') || '';
                        const body = await r.text();
                        results.push({
                            status: r.status,
                            url: url,
                            json: ct.includes('json'),
                            body: body.substring(0, 300)
                        });
                    } catch(e) {
                        results.push({status: 'ERR', url: url, body: e.message});
                    }
                }
                return results;
            }
        """, [parcel, token])

        for r in result:
            print(f"  {r['status']} {r['url']}")
            print(f"    {r['body'][:200]}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
