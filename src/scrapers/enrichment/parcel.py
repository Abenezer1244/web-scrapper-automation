"""County-agnostic parcel enrichment pipeline.

Pierce County: ATIP API with reCAPTCHA solving via 2Captcha + Playwright.
Correct endpoint: GET /api/parcelSearch?value=PARCEL_ID
with recaptcha-response header (requires browser session cookies).
"""

import requests

from src.api.middleware.security import add_scrape_domain
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment")

add_scrape_domain("atip.piercecountywa.gov")

# ─── Circuit breaker ─────────────────────────────────────────────────────────

_api_down: dict[str, bool] = {}


def _is_api_down(county_key: str) -> bool:
    return _api_down.get(county_key, False)


def _mark_api_down(county_key: str) -> None:
    _api_down[county_key] = True
    _logger.warning("Enrichment marked DOWN for %s — skipping remaining parcels", county_key)


# ─── Pierce County ATIP ───────────────────────────────────────────────────────

_ATIP_SITEKEY = "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT"
_ATIP_PAGE_URL = "https://atip.piercecountywa.gov/app/parcelSearch"
_EMPTY = {"property_address": None, "mailing_address": None}
_UNAVAILABLE = {"property_address": "(enrichment unavailable)", "mailing_address": "(enrichment unavailable)"}

# Shared Playwright scraper instance for batch enrichment
_shared_scraper = None
_shared_token = None


async def _get_atip_session():
    """Get or create a shared Playwright session for ATIP enrichment.

    Reuses the same browser context across multiple parcel lookups
    to avoid opening a new browser per parcel.
    """
    global _shared_scraper
    if _shared_scraper is None:
        from src.scrapers.base_scraper import BridgeScraper
        _shared_scraper = BridgeScraper()
        await _shared_scraper.__aenter__()
        await _shared_scraper.navigate("https://atip.piercecountywa.gov/app/parcelSearch")
        await _shared_scraper.page.wait_for_timeout(2_000)
        _logger.info("ATIP Playwright session established")
    return _shared_scraper


async def _close_atip_session():
    """Close the shared Playwright session."""
    global _shared_scraper
    if _shared_scraper:
        await _shared_scraper.__aexit__(None, None, None)
        _shared_scraper = None


async def _enrich_pierce_captcha(parcel_id: str) -> dict[str, str | None] | None:
    """Enrich via ATIP using Playwright + 2Captcha.

    1. Solve reCAPTCHA via 2Captcha (cached ~100s)
    2. Use shared Playwright session with session cookies
    3. Call /api/parcelSearch?value=PARCEL_ID from browser with token header
    4. Parse JSON response
    """
    global _shared_token
    from src.scrapers.enrichment.captcha import solve_recaptcha

    # Get or refresh CAPTCHA token
    if not _shared_token:
        _shared_token = await solve_recaptcha(_ATIP_PAGE_URL, _ATIP_SITEKEY)
    if not _shared_token:
        return None

    try:
        scraper = await _get_atip_session()

        result = await scraper.page.evaluate("""
            async (args) => {
                const [parcelId, captchaToken] = args;
                try {
                    const r = await fetch('/api/parcelSearch?value=' + parcelId, {
                        headers: {
                            'Accept': 'application/json',
                            'recaptcha-response': captchaToken
                        }
                    });
                    if (r.status !== 200) return {error: 'status ' + r.status};
                    const data = await r.json();
                    return {data: data};
                } catch(e) {
                    return {error: e.message};
                }
            }
        """, [parcel_id, _shared_token])

        if result.get("error"):
            err = result["error"]
            _logger.warning("ATIP API error for %s: %s", parcel_id, err)
            # If 500/401, token might be expired — clear it for refresh
            if "500" in str(err) or "401" in str(err):
                _shared_token = None
            return None

        data = result.get("data", [])
        return _parse_atip_response(data)

    except Exception as exc:
        _logger.warning("ATIP enrichment failed for %s: %s", parcel_id, str(exc)[:80])
        return None


def _parse_atip_response(data) -> dict[str, str | None]:
    """Parse ATIP /api/parcelSearch response.

    Response is a JSON array:
    [{"parcelNumber":"5000190130","line1":"2909 GALLEON CT NE","name":"MARCUS GRACE D",...}]
    """
    if not data or not isinstance(data, list) or len(data) == 0:
        return _EMPTY

    parcel = data[0]
    property_address = (parcel.get("line1") or "").strip() or None
    # ATIP search doesn't return a separate mailing address in the list response
    # Use the owner name as supplemental info
    mailing_address = None

    if property_address:
        _logger.info("Enriched: %s → %s", parcel.get("parcelNumber"), property_address)

    return {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }


async def _enrich_pierce_api_simple(parcel_id: str) -> dict[str, str | None] | None:
    """Quick check: try the ATIP API without CAPTCHA (in case it's restored)."""
    try:
        resp = requests.get(
            "https://atip.piercecountywa.gov/api/parcelSearch",
            params={"value": parcel_id},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        ct = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "json" in ct:
            data = resp.json()
            return _parse_atip_response(data)
    except Exception:
        pass
    return None


# ─── Public interface ─────────────────────────────────────────────────────────

async def enrich_parcel(parcel_id: str, county: str, state: str) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data."""
    county_key = f"{county.lower()}_{state.upper()}"

    if _is_api_down(county_key):
        return _UNAVAILABLE

    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    if county_key == "pierce_WA":
        # Try simple API first (in case CAPTCHA is removed)
        result = await _enrich_pierce_api_simple(parcel_id)
        if result and result.get("property_address"):
            return result

        # Try CAPTCHA-based enrichment
        from src.config import settings
        if settings.CAPTCHA_ENABLED and settings.CAPTCHA_API_KEY:
            result = await _enrich_pierce_captcha(parcel_id)
            if result is not None:
                return result

        _mark_api_down(county_key)
        return _UNAVAILABLE

    _logger.warning("No enrichment handler for %s — skipping", county_key)
    return _EMPTY
