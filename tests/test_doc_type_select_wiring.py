"""Phase B guard: every selectable pre_foreclosure county must resolve — through the
SAME registry path the worker uses — to a scraper whose constructor accepts ``doc_types``.

Codex (Critical): ``list_connectors`` surfaces doc-type checkboxes from the registry
alone, but the worker only passes ``doc_types`` to a scraper whose ``__init__`` accepts
it. If a county is flipped ``supported_for_selection=True`` before its scraper is wired,
a user can save a selection the scraper silently ignores and it scrapes EVERY document
type.

This guard resolves each selectable county through ``connector_scraper_class`` (the real
resolver: manual mode imports the configured class; ai mode runs ``_detect_template`` on
the base_url) using the county's REAL connector config, then asserts the resolved class
accepts ``doc_types``. So CI fails if the configured ``scraper_class``/``scraper_mode``/
``base_url`` routes to a scraper that drops the selection — not merely if a hand map is
stale.

When you enable a county, add its REAL connector config to ``_CONNECTOR_CONFIG`` (county,
state, scraper_mode, scraper_class for manual / base_url for ai), mirroring the prod
``county_connectors`` row.

Needs a synthetic env to import scraper modules (they pull settings). Run with throwaway
SECRET_KEY/DATABASE_URL/REDIS_URL — never the prod .env (conftest wipes tables).
"""
import inspect
from types import SimpleNamespace

from src.scrapers.doc_types import _AVAILABILITY, _EAGLEWEB_COUNTIES, availability_for
from src.scrapers.registry import connector_scraper_class

# Real county_connectors config per selectable county. These mirror the ACTIVE prod
# rows (verified live against bridgeleads-production 2026-06-23 — not the stale migration
# 006 seed, which left a dead inactive clark row pointing at the old King subclass). For
# manual mode, scraper_class is imported; for ai mode, base_url drives _detect_template.
_CONNECTOR_CONFIG: dict[tuple[str, str], dict] = {
    ("king", "wa"): {
        "scraper_mode": "manual",
        "scraper_class": "src.scrapers.king_wa_probate.KingCountyLandmarkWebScraper",
        "base_url": "https://dja-prd-ecexap1.kingcounty.gov/node/411",
    },
    ("pierce", "wa"): {
        "scraper_mode": "manual",
        "scraper_class": "src.scrapers.pierce_wa_probate.PierceWAARMSScraper",
        "base_url": "https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx",
    },
    ("clark", "wa"): {
        "scraper_mode": "manual",
        "scraper_class": "src.scrapers.clark_wa.ClarkWAScraper",
        "base_url": "https://e-docs.clark.wa.gov/LandmarkWeb",
    },
    # P2+: skagit/eagleweb/acclaim/idoc/laserfiche/tyler (ai -> template via
    # base_url), whatcom (manual whatcom_wa).
}


def _selectable_counties() -> list[tuple[str, str]]:
    """All (county,state) that currently expose the doc-type selector."""
    candidates = set(_AVAILABILITY.keys()) | {(c, "wa") for c in _EAGLEWEB_COUNTIES}
    return [
        (county, state)
        for county, state in candidates
        if (availability_for(county, state) or {}).get("supported_for_selection")
    ]


def test_every_selectable_county_resolves_to_doc_types_aware_scraper():
    selectable = _selectable_counties()
    assert selectable, "expected at least king + pierce to be selectable"
    for key in selectable:
        assert key in _CONNECTOR_CONFIG, (
            f"{key} is supported_for_selection=True but has no connector config in "
            f"_CONNECTOR_CONFIG — register its real county_connectors row before enabling"
        )
        county, state = key
        fake = SimpleNamespace(county=county, state=state, **_CONNECTOR_CONFIG[key])
        cls = connector_scraper_class(fake)
        assert cls is not None, (
            f"{key} connector config did not resolve to a scraper class via the real "
            f"registry resolver (bad scraper_class/base_url/mode?)"
        )
        params = inspect.signature(cls.__init__).parameters
        assert "doc_types" in params, (
            f"{key} resolves to {cls.__name__}.__init__ which does NOT accept doc_types — "
            f"the worker will silently scrape ALL document types. Wire it before enabling."
        )
