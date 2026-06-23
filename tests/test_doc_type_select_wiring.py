"""Phase B guard: every selectable pre_foreclosure county must resolve to a scraper
whose constructor accepts ``doc_types``.

Codex (Critical): ``list_connectors`` surfaces doc-type checkboxes from the registry
alone, but the worker only passes ``doc_types`` to a scraper whose ``__init__``
signature accepts it. If a county is flipped ``supported_for_selection=True`` before
its scraper is wired, a user can save a selection the scraper silently ignores and it
scrapes EVERY document type. This guard makes that mistake fail in CI: when you enable
a new county you MUST register its wired scraper class here, and that class must accept
``doc_types``.

Needs a synthetic env to import scraper modules (they pull settings). Run with throwaway
SECRET_KEY/DATABASE_URL/REDIS_URL — never the prod .env (conftest wipes tables).
"""
import importlib
import inspect

from src.scrapers.doc_types import _AVAILABILITY, _EAGLEWEB_COUNTIES, availability_for

# When you flip supported_for_selection=True for a county (in _AVAILABILITY or via the
# EagleWeb template path), add it here pointing at the scraper class that actually runs
# it. ai-mode counties resolve to their recorder-platform template class; manual-mode
# counties to their hand-coded class. This map is the contract the guard enforces.
_SCRAPER_FOR_SELECTABLE_COUNTY: dict[tuple[str, str], str] = {
    ("king", "wa"): "src.scrapers.king_wa_probate.KingCountyLandmarkWebScraper",
    ("pierce", "wa"): "src.scrapers.pierce_wa_probate.PierceWAARMSScraper",
    # P1+: ("clark","wa") -> clark_wa.ClarkWAScraper, ("skagit","wa") -> skagit template,
    #      EagleWeb counties -> templates.eagleweb.EagleWebScraper, etc.
}


def _selectable_counties() -> list[tuple[str, str]]:
    """All (county,state) that currently expose the doc-type selector."""
    candidates = set(_AVAILABILITY.keys()) | {(c, "wa") for c in _EAGLEWEB_COUNTIES}
    out = []
    for county, state in candidates:
        a = availability_for(county, state)
        if a is not None and a.get("supported_for_selection"):
            out.append((county, state))
    return out


def test_every_selectable_county_is_registered_and_wired():
    selectable = _selectable_counties()
    assert selectable, "expected at least king + pierce to be selectable"
    for key in selectable:
        assert key in _SCRAPER_FOR_SELECTABLE_COUNTY, (
            f"{key} is supported_for_selection=True but has no scraper registered in "
            f"_SCRAPER_FOR_SELECTABLE_COUNTY — wire its scraper before enabling the county"
        )
        path = _SCRAPER_FOR_SELECTABLE_COUNTY[key]
        module_path, class_name = path.rsplit(".", 1)
        klass = getattr(importlib.import_module(module_path), class_name)
        params = inspect.signature(klass.__init__).parameters
        assert "doc_types" in params, (
            f"{path}.__init__ must accept a doc_types parameter so the worker can pass "
            f"the user's selection (county {key} is selectable)"
        )
