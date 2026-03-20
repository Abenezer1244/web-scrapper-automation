"""County-agnostic parcel enrichment pipeline.

Priority order:
1. Regrid national API (works for ALL US counties, no CAPTCHA)
2. County-specific fallback (Pierce County ATIP with CAPTCHA)
3. Return "(enrichment unavailable)"
"""

from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment")

_EMPTY = {"property_address": None, "mailing_address": None}
_UNAVAILABLE = {
    "property_address": "(enrichment unavailable)",
    "mailing_address": "(enrichment unavailable)",
}

# Circuit breaker per source
_source_down: dict[str, bool] = {}


async def enrich_parcel(parcel_id: str, county: str, state: str) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data.

    Uses Regrid national API (all US counties) as primary source.
    Falls back to county-specific enrichment if Regrid is unavailable.
    """
    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    # ── Primary: Regrid national API (all counties) ───────────────────────
    from src.config import settings

    if settings.REGRID_ENABLED and settings.REGRID_API_TOKEN:
        if not _source_down.get("regrid"):
            from src.scrapers.enrichment.national import enrich_parcel_national

            result = enrich_parcel_national(parcel_id, county, state)
            if result.get("property_address"):
                return result

            # If Regrid returned nothing, don't mark as down (might just be unknown parcel)
            _logger.info("Regrid: no data for parcel %s", parcel_id)

    # ── Fallback: county-specific enrichment ──────────────────────────────
    # Pierce County: ATIP with CAPTCHA (legacy, expensive)
    county_key = f"{county.lower()}_{state.upper()}"
    if county_key == "pierce_WA" and not _source_down.get(county_key):
        if settings.CAPTCHA_ENABLED and settings.CAPTCHA_API_KEY:
            try:
                from src.scrapers.base_scraper import BridgeScraper
                from src.scrapers.enrichment.captcha import solve_recaptcha

                sitekey = "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT"
                token = await solve_recaptcha(
                    "https://atip.piercecountywa.gov/app/parcelSearch",
                    sitekey,
                )
                if token:
                    async with BridgeScraper() as scraper:
                        await scraper.navigate("https://atip.piercecountywa.gov/app/parcelSearch")
                        await scraper.page.wait_for_timeout(2_000)

                        api_result = await scraper.page.evaluate("""
                            async (args) => {
                                const [pid, tok] = args;
                                try {
                                    const r = await fetch('/api/parcelSearch?value=' + pid, {
                                        headers: {'Accept':'application/json','recaptcha-response':tok}
                                    });
                                    if (r.status !== 200) return null;
                                    const data = await r.json();
                                    if (!data || !data.length) return null;
                                    return {address: (data[0].line1 || '').trim(), name: (data[0].name || '').trim()};
                                } catch(e) { return null; }
                            }
                        """, [parcel_id, token])

                        if api_result and api_result.get("address"):
                            return {
                                "property_address": api_result["address"],
                                "mailing_address": api_result["address"],
                            }
            except Exception as exc:
                _logger.warning("ATIP fallback failed: %s", str(exc)[:60])

        _source_down[county_key] = True
        return _UNAVAILABLE

    return _UNAVAILABLE
