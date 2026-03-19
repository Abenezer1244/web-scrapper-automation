"""County-agnostic parcel enrichment pipeline.

Pierce County: ATIP with reCAPTCHA solving via 2Captcha + Playwright.
Flow: solve reCAPTCHA → inject token into ATIP page → scrape rendered address.
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


async def _enrich_pierce_captcha(parcel_id: str) -> dict[str, str | None] | None:
    """Enrich via ATIP using Playwright + 2Captcha to solve reCAPTCHA.

    1. Solve reCAPTCHA via 2Captcha (cached ~100s)
    2. Open ATIP in Playwright, inject the token
    3. Navigate to parcel search, wait for results to render
    4. Scrape the address from the rendered page
    """
    from src.scrapers.enrichment.captcha import solve_recaptcha

    token = await solve_recaptcha(_ATIP_PAGE_URL, _ATIP_SITEKEY)
    if not token:
        return None

    from src.scrapers.base_scraper import BridgeScraper

    url = f"https://atip.piercecountywa.gov/app/parcelSearch?parcelNumber={parcel_id}"

    try:
        async with BridgeScraper() as scraper:
            await scraper.navigate(url)

            # Inject the solved reCAPTCHA token
            await scraper.page.evaluate(f"""
                () => {{
                    // Set the reCAPTCHA response in the hidden textarea
                    const ta = document.querySelector('#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
                    if (ta) {{
                        ta.value = '{token}';
                    }}
                    // Also try to call the reCAPTCHA callback if it exists
                    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {{
                        for (const client of Object.values(window.___grecaptcha_cfg.clients)) {{
                            const callback = client?.rr?.l?.callback;
                            if (callback) callback('{token}');
                        }}
                    }}
                }}
            """)

            # Wait for the Angular SPA to load parcel data
            await scraper.page.wait_for_timeout(5_000)

            # Scrape the rendered page for address data
            soup = await scraper.get_soup_async()

        return _parse_atip_html(soup)

    except Exception as exc:
        _logger.warning("Playwright enrichment failed for %s: %s", parcel_id, str(exc)[:80])
        return None


def _parse_atip_html(soup) -> dict[str, str | None]:
    """Extract addresses from the rendered ATIP page HTML."""
    property_address = None
    mailing_address = None

    soup.get_text(separator="\n")

    # Look for address patterns in the rendered page
    for label_el in soup.select("dt, th, label, .label, strong, b"):
        label_text = (label_el.get_text(strip=True) or "").lower()
        sibling = label_el.find_next_sibling()
        if not sibling:
            # Try next element
            sibling = label_el.find_next()
        if not sibling:
            continue
        value = sibling.get_text(separator=" ", strip=True)
        if not value or len(value) > 200:
            continue

        if any(w in label_text for w in ["site address", "situs", "property address", "location"]):
            property_address = value
        elif "mailing" in label_text:
            mailing_address = value

    return {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }


async def _enrich_pierce_api_simple(parcel_id: str) -> dict[str, str | None] | None:
    """Quick check: try the ATIP API without CAPTCHA (in case it's restored)."""
    try:
        resp = requests.get(
            "https://atip.piercecountywa.gov/api/parcelSearch/search",
            params={"parcelNumber": parcel_id},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        ct = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "json" in ct:
            data = resp.json()
            parcel = data.get("parcel") or data
            site = parcel.get("siteAddress") or {}
            mail = parcel.get("mailingAddress") or {}

            def _join(*parts):
                return " ".join(str(p) for p in parts if p and str(p).strip()).strip() or None

            return {
                "property_address": _join(site.get("streetAddress"), site.get("city"), site.get("state"), site.get("zip")),
                "mailing_address": _join(mail.get("streetAddress"), mail.get("city"), mail.get("state"), mail.get("zip")),
            }
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
        if result and (result.get("property_address") or result.get("mailing_address")):
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
