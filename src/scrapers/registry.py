"""County connector registry.

Maps (county, state, record_type) → scraper class via the county_connectors DB table.
County connectors register themselves by calling add_scrape_domain() at import time
and being listed in the DB.
"""

import importlib

from sqlalchemy import func, select

from src.db.models import CountyConnector
from src.db.session import SyncSessionLocal
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.registry")


class UnsupportedCountyError(ValueError):
    """Raised when no active connector exists for a county/state/record_type combination."""


def get_scraper_class(county: str, state: str, record_type: str):
    """Look up and return the scraper class for the given county connector.

    Performs a case-insensitive lookup against the county_connectors table.

    Args:
        county: County slug (e.g. 'pierce').
        state: State code (e.g. 'WA').
        record_type: Record type slug (e.g. 'probate').

    Returns:
        The scraper class (a subclass of BridgeScraper).

    Raises:
        UnsupportedCountyError: If no active connector matches.
    """
    with SyncSessionLocal() as db:
        result = db.execute(
            select(CountyConnector).where(
                func.lower(CountyConnector.county) == county.lower(),
                func.lower(CountyConnector.state) == state.lower(),
                CountyConnector.active,
            )
        )
        connector = result.scalar_one_or_none()

    if connector is None:
        raise UnsupportedCountyError(
            f"No active connector for {county.lower()}, {state.upper()}"
        )

    if record_type.lower() not in [rt.lower() for rt in connector.record_types]:
        raise UnsupportedCountyError(
            f"Record type '{record_type}' not supported for {county}, {state}. "
            f"Supported: {connector.record_types}"
        )

    # AI-powered scraper: return AIScraper configured with the connector's base_url
    scraper_mode = getattr(connector, "scraper_mode", "manual")
    if scraper_mode == "ai":
        from functools import partial

        # Check for template match (saves Claude AI tokens)
        template_class = _detect_template(connector.base_url)
        if template_class:
            _logger.info(
                "Registry resolved %s/%s/%s → %s (template, base_url=%s)",
                county, state, record_type, template_class.__name__, connector.base_url,
            )
            return partial(
                template_class,
                base_url=connector.base_url,
                county=connector.county,
                state=connector.state,
                record_types=connector.record_types,
            )

        from src.scrapers.ai_scraper import AIScraper

        _logger.info(
            "Registry resolved %s/%s/%s → AIScraper (ai mode, base_url=%s)",
            county, state, record_type, connector.base_url,
        )
        # Return a factory that creates an AIScraper with the right config
        return partial(
            AIScraper,
            base_url=connector.base_url,
            county=connector.county,
            state=connector.state,
            record_types=connector.record_types,
        )

    # Manual mode: dynamically import the hand-coded scraper class
    # SECURITY: Only allow imports from pre-approved modules to prevent code injection
    _ALLOWED_SCRAPER_MODULES = frozenset([
        "src.scrapers.pierce_wa_probate",
        "src.scrapers.ai_scraper",
        "src.scrapers.base_scraper",
    ])

    module_path, class_name = connector.scraper_class.rsplit(".", 1)

    if module_path not in _ALLOWED_SCRAPER_MODULES:
        raise UnsupportedCountyError(
            f"Scraper module '{module_path}' not in allowlist"
        )

    try:
        module = importlib.import_module(module_path)
        scraper_class = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise UnsupportedCountyError(
            f"Failed to load scraper class '{connector.scraper_class}': {exc}"
        ) from exc

    _logger.info(
        "Registry resolved %s/%s/%s → %s (manual mode)",
        county, state, record_type, connector.scraper_class,
    )
    return scraper_class


def _detect_template(base_url: str):
    """Detect if a URL matches a known recorder platform template.

    Returns the template scraper class if matched, None otherwise.
    This saves Claude AI tokens by using standardized navigation
    for known platforms.
    """
    url_lower = base_url.lower()

    # EagleWeb (Tyler Technologies) — template scraper for all EagleWeb sites.
    # With Xvfb virtual display, Playwright runs in headed mode which fixes
    # the JS redirect issue on docSearchPOST.jsp.
    # Tyler Self-Service uses /web/ paths on tylerhost.net — exclude from EagleWeb
    selfservice_patterns = ["/web/user/disclaimer", "/web/search/", "selfservice."]
    if any(p in url_lower for p in selfservice_patterns):
        return None  # Fall through to AI scraper

    eagleweb_patterns = [
        "/recorder/web",
        "recorder/web",     # also matches /thurstonrecorder/web, /grantrecorder/web
        "/eagleweb/",
        "eagleweb.",         # matches eagleweb.co.thurston.wa.us (domain)
        "tylerhost.net",
        "countygovernmentrecords.com",
    ]
    if any(p in url_lower for p in eagleweb_patterns):
        from src.scrapers.templates.eagleweb import EagleWebScraper
        return EagleWebScraper

    # LandmarkWeb (Hyland) — King County (and potentially Clark, Snohomish)
    # URL pattern: /LandmarkWeb/
    if "/landmarkweb" in url_lower:
        from src.scrapers.templates.landmarkweb import LandmarkWebScraper
        return LandmarkWebScraper

    # AVA Fidlar — Yakima
    if "ava.fidlar.com" in url_lower or "/avaWeb" in url_lower or "/avaweb" in url_lower:
        from src.scrapers.templates.ava_fidlar import AvaFidlarScraper
        return AvaFidlarScraper

    # AcclaimWeb (Tyler) — Chelan, Douglas, Pend Oreille
    if "/acclaimweb" in url_lower:
        from src.scrapers.templates.acclaimweb import AcclaimWebScraper
        return AcclaimWebScraper

    return None


def list_supported() -> list[dict]:
    """Return all active county connectors with their supported record types.

    Used to populate the county picker in the frontend wizard.
    """
    with SyncSessionLocal() as db:
        result = db.execute(
            select(CountyConnector).where(CountyConnector.active)
            .order_by(CountyConnector.state, CountyConnector.county)
        )
        connectors = result.scalars().all()

    return [
        {
            "county": c.county,
            "state": c.state,
            "record_types": c.record_types,
            "health_status": c.health_status,
            "base_url": c.base_url,
            "scraper_mode": getattr(c, "scraper_mode", "manual"),
        }
        for c in connectors
    ]
