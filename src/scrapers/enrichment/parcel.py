"""County-agnostic parcel enrichment pipeline.

Pierce County: ATIP API with reCAPTCHA solving via 2Captcha.
The ATIP API requires a `recaptcha-response` header on every call.
We solve the reCAPTCHA once and reuse the token for ~2 minutes.
"""

import asyncio
import json
from typing import Any

import requests

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment")

add_scrape_domain("atip.piercecountywa.gov")

# ─── Circuit breaker ─────────────────────────────────────────────────────────

_api_down: dict[str, bool] = {}


def _is_api_down(county_key: str) -> bool:
    return _api_down.get(county_key, False)


def _mark_api_down(county_key: str) -> None:
    _api_down[county_key] = True
    _logger.warning("Enrichment API marked DOWN for %s — skipping remaining parcels", county_key)


# ─── Pierce County ATIP ───────────────────────────────────────────────────────

_ATIP_RECAPTCHA_URL = "https://atip.piercecountywa.gov/api/publicRecaptcha"
_ATIP_SITEKEY = "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT"
_ATIP_PAGE_URL = "https://atip.piercecountywa.gov/app/parcelSearch"

_EMPTY = {"property_address": None, "mailing_address": None}
_UNAVAILABLE = {"property_address": "(enrichment unavailable)", "mailing_address": "(enrichment unavailable)"}


def _parse_atip_response(data) -> dict[str, str | None]:
    """Extract addresses from ATIP API response.

    The response can be a list of parcels or a single parcel object.
    """
    # Handle list response
    if isinstance(data, list):
        if not data:
            return _EMPTY
        parcel_data = data[0]
    elif isinstance(data, dict):
        parcel_data = data
    else:
        return _EMPTY

    # Navigate nested structure
    parcel = parcel_data.get("parcel") or parcel_data
    site = parcel.get("siteAddress") or parcel.get("situs") or {}
    mail = parcel.get("mailingAddress") or parcel.get("mailing") or {}

    def _join(*parts) -> str | None:
        joined = " ".join(str(p) for p in parts if p and str(p).strip())
        return joined.strip() or None

    property_address = _join(
        site.get("streetAddress") or site.get("address"),
        site.get("city"),
        site.get("state"),
        site.get("zip") or site.get("zipCode"),
    )
    mailing_address = _join(
        mail.get("streetAddress") or mail.get("address"),
        mail.get("city"),
        mail.get("state"),
        mail.get("zip") or mail.get("zipCode"),
    )
    return {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }


async def _enrich_pierce_api(parcel_id: str) -> dict[str, str | None] | None:
    """Fetch parcel data from Pierce County ATIP with reCAPTCHA solving.

    Flow:
    1. Get a solved reCAPTCHA token (from cache or 2Captcha)
    2. Call ATIP publicRecaptcha endpoint with the token as header
    3. Parse the JSON response for addresses
    """
    # Step 1: Get reCAPTCHA token
    from src.scrapers.enrichment.captcha import solve_recaptcha, invalidate_token

    token = await solve_recaptcha(_ATIP_PAGE_URL, _ATIP_SITEKEY)
    if not token:
        _logger.warning("Could not solve reCAPTCHA — enrichment unavailable")
        return None

    # Step 2: Call ATIP API with token
    filter_param = json.dumps({"criteria": [{"eq": ["parcelNumber", parcel_id]}]})
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "recaptcha-response": token,
    }

    try:
        resp = requests.get(
            _ATIP_RECAPTCHA_URL,
            params={
                "bypass-recaptcha-interceptor": "true",
                "filter": filter_param,
            },
            headers=headers,
            timeout=15,
        )

        content_type = resp.headers.get("content-type", "")

        if resp.status_code == 200 and "json" in content_type:
            data = resp.json()
            result = _parse_atip_response(data)
            if result.get("property_address") or result.get("mailing_address"):
                _logger.info("Enriched parcel %s: %s", parcel_id, result.get("property_address", "N/A"))
                return result
            # API returned JSON but no address data
            _logger.warning("ATIP returned no address data for parcel %s", parcel_id)
            return _EMPTY

        if resp.status_code == 500:
            # Token might be expired or invalid
            _logger.warning("ATIP returned 500 — reCAPTCHA token may be expired, invalidating")
            invalidate_token(_ATIP_SITEKEY)
            # Retry once with a fresh token
            token = await solve_recaptcha(_ATIP_PAGE_URL, _ATIP_SITEKEY)
            if token:
                headers["recaptcha-response"] = token
                resp = requests.get(
                    _ATIP_RECAPTCHA_URL,
                    params={"bypass-recaptcha-interceptor": "true", "filter": filter_param},
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                    return _parse_atip_response(resp.json())

            return None

        if "html" in content_type:
            _logger.warning("ATIP returned HTML — API may be down")
            return None

        _logger.warning("ATIP returned %d for parcel %s", resp.status_code, parcel_id)
        return None

    except requests.exceptions.JSONDecodeError:
        _logger.warning("ATIP returned non-JSON for parcel %s", parcel_id)
        return None
    except requests.exceptions.Timeout:
        _logger.warning("ATIP timed out for parcel %s", parcel_id)
        return None
    except Exception as exc:
        _logger.warning("ATIP error for parcel %s: %s", parcel_id, exc)
        return None


# ─── Public interface ─────────────────────────────────────────────────────────

async def enrich_parcel(parcel_id: str, county: str, state: str) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data."""
    county_key = f"{county.lower()}_{state.upper()}"

    if _is_api_down(county_key):
        return _UNAVAILABLE

    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    if county_key == "pierce_WA":
        result = await _enrich_pierce_api(parcel_id)
        if result is None:
            _mark_api_down(county_key)
            return _UNAVAILABLE
        return result

    _logger.warning("No enrichment handler for %s — skipping", county_key)
    return _EMPTY
