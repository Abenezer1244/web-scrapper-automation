"""County connector registry.

Maps (county, state, record_type) → scraper class via the county_connectors DB table.
County connectors register themselves by calling add_scrape_domain() at import time
and being listed in the DB.
"""

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from src.db.models import CountyConnector
from src.db.session import SyncSessionLocal
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.scrapers.base_scraper import BridgeScraper

_logger = setup_logger("scraper.registry")


class UnsupportedCountyError(ValueError):
    """Raised when no active connector exists for a county/state/record_type combination."""


# SECURITY: only allow scraper-class imports from pre-approved modules to prevent
# code injection via the DB-stored scraper_class string. Shared by get_scraper_class
# (execution) and connector_scraper_class (read-only metadata).
_ALLOWED_SCRAPER_MODULES = frozenset([
    "src.scrapers.pierce_wa_probate",
    "src.scrapers.pierce_wa_code_violation",
    "src.scrapers.clark_wa",
    "src.scrapers.whatcom_wa",
    "src.scrapers.king_wa_code_violation",
    "src.scrapers.king_wa_tax_delinquent",
    "src.scrapers.king_wa_probate",
    "src.scrapers.snohomish_wa_tax_delinquent",
    "src.scrapers.snohomish_wa_pre_foreclosure",
    "src.scrapers.trustee_sale",
    "src.scrapers.base_scraper",
])


# Either a BridgeScraper subclass or a functools.partial that returns one.
# Both are callables that, when invoked with no positional args, yield a
# BridgeScraper instance — that's all the caller needs.
ScraperFactory = Callable[..., "BridgeScraper"]


def get_scraper_class(
    county: str, state: str, record_type: str
) -> tuple[ScraperFactory, str]:
    """Look up and return the scraper class for the given county connector.

    Performs a case-insensitive lookup against the county_connectors table.

    Args:
        county: County slug (e.g. 'pierce').
        state: State code (e.g. 'WA').
        record_type: Record type slug (e.g. 'probate').

    Returns:
        Tuple of (scraper_class, record_type) — the scraper class (a subclass
        of BridgeScraper) and the matched record_type string. Callers should
        pass record_type when instantiating: scraper_class(record_type=record_type).

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
        connectors = result.scalars().all()

    if not connectors:
        raise UnsupportedCountyError(
            f"No active connector for {county.lower()}, {state.upper()}"
        )

    # Find the connector that supports this specific record type
    connector = None
    all_types = []
    for c in connectors:
        all_types.extend(c.record_types)
        if record_type.lower() in [rt.lower() for rt in c.record_types]:
            connector = c
            break

    if connector is None:
        raise UnsupportedCountyError(
            f"Record type '{record_type}' not supported for {county}, {state}. "
            f"Supported: {list(set(all_types))}"
        )

    # AI-mode connector: resolve to the recorder-platform TEMPLATE that matches
    # its base_url. "ai" mode means "detect the platform template from the URL" —
    # NOT "run a generic LLM navigator". The old generic AIScraper fallback was
    # removed: it produced low-quality unstructured output and every active
    # ai-mode connector now maps to a concrete template. If no template matches,
    # fail closed (UnsupportedCountyError) rather than silently degrade.
    scraper_mode = getattr(connector, "scraper_mode", "manual")
    if scraper_mode == "ai":
        from functools import partial

        template_class = _detect_template(connector.base_url)
        if template_class is None:
            raise UnsupportedCountyError(
                f"No scraper template matches base_url {connector.base_url!r} for "
                f"{county}, {state} (ai-mode connector with no recognized recorder "
                "platform; the generic AI scraper has been removed)"
            )
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
        ), record_type

    # Manual mode: dynamically import the hand-coded scraper class from the
    # module-level allowlist (_ALLOWED_SCRAPER_MODULES).
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
    return scraper_class, record_type


def connector_scraper_class(connector) -> type | None:
    """Resolve a CountyConnector row to its scraper CLASS (not a partial), without a
    DB lookup and without raising.

    For read-only metadata callers (e.g. the SHOW collection_scope display) that
    already hold the connector row and just need to query a classmethod. ai-mode
    resolves to the recorder-platform template; manual mode imports the allowlisted
    class. Returns None when the connector cannot be resolved.
    """
    if getattr(connector, "scraper_mode", "manual") == "ai":
        return _detect_template(connector.base_url or "")
    scraper_class = getattr(connector, "scraper_class", None)
    if not scraper_class or "." not in scraper_class:
        return None
    module_path, class_name = scraper_class.rsplit(".", 1)
    if module_path not in _ALLOWED_SCRAPER_MODULES:
        return None
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError):
        return None


def _detect_template(base_url: str):
    """Detect if a URL matches a known recorder platform template.

    Returns the template scraper class if matched, None otherwise.
    This saves Claude AI tokens by using standardized navigation
    for known platforms.
    """
    url_lower = base_url.lower()

    # EagleWeb (Tyler Technologies) — template scraper for EagleWeb sites.
    # Check EagleWeb patterns FIRST because some installations sit on the
    # tylerhost.net domain but use the /{county}recorder/web/ path layout
    # (e.g. grant: grantcountywa-recorder.tylerhost.net/grantrecorder/web/).
    # Those are EagleWeb even though they share the tylerhost.net domain
    # with Tyler SelfService.
    eagleweb_patterns = [
        "/recorder/web",
        "recorder/web",     # also matches /thurstonrecorder/web, /grantrecorder/web
        "/eagleweb/",
        "eagleweb.",         # matches eagleweb.co.thurston.wa.us (domain)
        "countygovernmentrecords.com",
    ]
    if any(p in url_lower for p in eagleweb_patterns):
        from src.scrapers.templates.eagleweb import EagleWebScraper
        return EagleWebScraper

    # Tyler SelfService — a separate platform despite the shared vendor.
    # URLs end in `/Web` (case-insensitive in the url_lower check) and the
    # portal subdomain is either `*.tylerhost.net` or `selfservice.*`.
    # Matches okanogan (tylerhost.net/Web), lincoln (tylerhost.net/web/...),
    # stevens (selfservice.stevenscountywa.gov/web). Grant is NOT matched
    # here — its /grantrecorder/web/ path was already caught above.
    if "tylerhost.net" in url_lower or "selfservice." in url_lower:
        from src.scrapers.templates.tyler_selfservice import TylerSelfServiceScraper
        return TylerSelfServiceScraper

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

    # Skagit County Recording Search — custom ASP.NET portal
    if "skagitcounty.net" in url_lower and "recording" in url_lower:
        from src.scrapers.templates.skagit_recording import SkagitRecordingScraper
        return SkagitRecordingScraper

    # Laserfiche WebLink — Cowlitz (and potentially others)
    if "wlaudpublic" in url_lower or "laserfiche" in url_lower or "weblink" in url_lower:
        from src.scrapers.templates.laserfiche_weblink import LaserficheWebLinkScraper
        return LaserficheWebLinkScraper

    # iDocMarket (Tyler) — Columbia (COLWA1). base_url is the full per-county
    # search URL (e.g. .../COLWA1/Document/Search); the county code lives in the
    # URL so the template is generic across iDocMarket counties.
    if "idocmarket.com" in url_lower:
        from src.scrapers.templates.idocmarket import IDocMarketScraper
        return IDocMarketScraper

    return None


def has_template(base_url: str) -> bool:
    """True if ``base_url`` maps to a known recorder-platform template.

    Public predicate for callers (e.g. the connector-create API) that need to
    know, without importing the private detector, whether an ai-mode connector
    pointed at this URL would resolve to a template or fail closed.
    """
    return _detect_template(base_url or "") is not None


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
