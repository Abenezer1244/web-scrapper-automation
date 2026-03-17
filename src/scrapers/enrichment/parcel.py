"""County-agnostic parcel enrichment pipeline.

Each county connector calls enrich_parcel() with a parcel_id.
Phase 1 implementation: Pierce County ATIP REST API with Playwright UI fallback.
New counties plug in their own lookup while keeping the same interface.
"""

import asyncio
from typing import Any

import requests

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment")

# Register approved enrichment domains (SSRF allowlist)
add_scrape_domain("atip.piercecountywa.gov")

# ─── Pierce County ATIP ───────────────────────────────────────────────────────

_ATIP_API_URL = "https://atip.piercecountywa.gov/app/v2/parcelSearch/search"
_ATIP_HEADERS = {
    "User-Agent": "BridgeLeads-Enrichment/1.0",
    "Accept": "application/json",
}


def _parse_atip_response(data: dict[str, Any]) -> dict[str, str | None]:
    """Extract property_address and mailing_address from ATIP API response."""
    parcel = data.get("parcel") or {}
    site = parcel.get("siteAddress") or {}
    mail = parcel.get("mailingAddress") or {}

    def _join(*parts: str | None) -> str | None:
        joined = " ".join(p for p in parts if p and str(p).strip())
        return joined.strip() or None

    property_address = _join(
        site.get("streetAddress"),
        site.get("city"),
        site.get("state"),
        site.get("zip"),
    )
    mailing_address = _join(
        mail.get("streetAddress"),
        mail.get("city"),
        mail.get("state"),
        mail.get("zip"),
    )
    return {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }


async def _enrich_pierce_api(parcel_id: str) -> dict[str, str | None] | None:
    """Fetch parcel data from Pierce County ATIP REST API.

    Returns parsed address dict on success, None if the API is unavailable.
    Applies exponential backoff on HTTP 429.
    """
    params = {"parcelNumber": parcel_id}

    for attempt in range(1, settings.MAX_RETRIES + 1):
        try:
            resp = requests.get(
                _ATIP_API_URL,
                params=params,
                headers=_ATIP_HEADERS,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return _parse_atip_response(data)
            if resp.status_code == 429:
                wait = 2 ** attempt
                _logger.warning("ATIP rate-limited (attempt %d). Waiting %ds.", attempt, wait)
                await asyncio.sleep(wait)
                continue
            _logger.warning("ATIP API returned %d for parcel %s", resp.status_code, parcel_id)
            return None
        except Exception as exc:
            _logger.warning("ATIP API error on attempt %d: %s", attempt, exc)
            if attempt < settings.MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

    return None


async def _enrich_pierce_playwright(parcel_id: str) -> dict[str, str | None]:
    """Playwright UI fallback for Pierce County ATIP when the API is unavailable."""
    from src.scrapers.base_scraper import BridgeScraper

    url = f"https://atip.piercecountywa.gov/app/parcelSearch?parcelNumber={parcel_id}"

    async with BridgeScraper() as scraper:
        await scraper.navigate(url)
        soup = await scraper.get_soup_async()

    property_address: str | None = None
    mailing_address: str | None = None

    # ATIP renders addresses in labeled dl/dd elements
    for label_el in soup.select("dt, th, label"):
        label_text = (label_el.get_text(strip=True) or "").lower()
        sibling = label_el.find_next_sibling(["dd", "td"])
        if not sibling:
            continue
        value = sibling.get_text(separator=" ", strip=True)
        if "site" in label_text or "property" in label_text:
            property_address = value
        elif "mail" in label_text:
            mailing_address = value

    return {"property_address": property_address, "mailing_address": mailing_address}


# ─── Public interface ─────────────────────────────────────────────────────────

async def enrich_parcel(parcel_id: str, county: str, state: str) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data.

    Routes to the correct county lookup based on county + state.
    New counties implement their own async function and are added to the
    dispatch table below — no changes needed to the caller.

    Args:
        parcel_id: The county parcel identifier (e.g. '0001000001').
        county: Lowercase county slug (e.g. 'pierce').
        state: Uppercase state code (e.g. 'WA').

    Returns:
        Dict with keys: property_address, mailing_address (either may be None).
    """
    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    county_key = f"{county.lower()}_{state.upper()}"

    if county_key == "pierce_WA":
        result = await _enrich_pierce_api(parcel_id)
        if result is None:
            _logger.info("ATIP API unavailable — falling back to Playwright UI for parcel %s", parcel_id)
            result = await _enrich_pierce_playwright(parcel_id)
        return result

    # Unknown county — return empty enrichment rather than error
    _logger.warning("No enrichment handler for %s — skipping", county_key)
    return {"property_address": None, "mailing_address": None}
