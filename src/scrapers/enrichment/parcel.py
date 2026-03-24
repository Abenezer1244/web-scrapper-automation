"""County-agnostic parcel enrichment pipeline.

Priority order (cost-optimized):
1. County GIS REST API (FREE — no API key, no CAPTCHA)
2. Regrid national API (paid — $375/mo, disabled by default)
3. AI assessor scraper (Claude API — ~$0.01/lookup, cached)
4. County-specific fallback (Pierce County ATIP with CAPTCHA)
5. Return "(enrichment unavailable)"
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


async def enrich_parcel(
    parcel_id: str, county: str, state: str, owner_name: str | None = None
) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data.

    Tries sources in order of cost (cheapest first):
    1. County GIS REST API (free) — by parcel ID, then by owner name
    2. Regrid national API (paid, if enabled)
    3. AI assessor scraper (Claude API)
    4. County-specific fallback (ATIP for Pierce)
    """
    # Skip enrichment if no parcel ID — don't waste time/money on Regrid/AI/ATIP
    if not parcel_id or len(parcel_id.strip()) < 5:
        _logger.info("Skipping enrichment — no parcel ID (%s, %s)", county, state)
        return _EMPTY

    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    from src.config import settings

    # ── 1. County GIS REST API (FREE) ────────────────────────────────────────
    if settings.GIS_ENRICHMENT_ENABLED and not _source_down.get("gis"):
        try:
            from src.scrapers.enrichment.county_gis import enrich_parcel_gis

            result = enrich_parcel_gis(parcel_id, county, state, owner_name=owner_name)
            if result.get("property_address"):
                _logger.info("GIS enrichment succeeded for parcel %s", parcel_id)
                return result

            _logger.info("GIS: no data for parcel %s", parcel_id)
        except Exception as exc:
            _logger.warning("GIS enrichment error: %s", str(exc)[:60])

    # ── 2. Regrid national API (paid, if enabled) ────────────────────────────
    if settings.REGRID_ENABLED and settings.REGRID_API_TOKEN:
        if not _source_down.get("regrid"):
            from src.scrapers.enrichment.national import enrich_parcel_national

            result = enrich_parcel_national(parcel_id, county, state)
            if result.get("property_address"):
                return result

            _logger.info("Regrid: no data for parcel %s", parcel_id)

    # ── 3. AI assessor scraper (Claude API, ~$0.01/lookup) ───────────────────
    if settings.AI_ENRICHMENT_ENABLED and settings.ANTHROPIC_API_KEY:
        if not _source_down.get("ai_assessor"):
            try:
                from src.scrapers.enrichment.ai_assessor import enrich_parcel_ai

                result = await enrich_parcel_ai(parcel_id, county, state)
                if result.get("property_address"):
                    _logger.info("AI assessor enrichment succeeded for parcel %s", parcel_id)
                    return result

                _logger.info("AI assessor: no data for parcel %s", parcel_id)
            except Exception as exc:
                _logger.warning("AI assessor error: %s", str(exc)[:60])

    # ── 4. County-specific fallback (Pierce ATIP + CAPTCHA) ──────────────────
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
