"""Migration 081 (trustee_sale connectors) — the seed data must be internally valid.

No DB: loads the migration module and asserts its connector rows point at REAL,
allowlisted scraper subclasses whose COUNTY matches the row's county. This catches a
scraper_class typo / county mismatch that would otherwise only surface as an
"unsupported county" 422 at runtime.
"""
import importlib
import importlib.util
from pathlib import Path

from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import _TrusteeSaleScraper

_MIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "081_add_trustee_sale_connectors.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig081", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chains_onto_current_head():
    mig = _load_migration()
    assert mig.revision == "081"
    assert mig.down_revision == "080"


def test_seeds_exactly_the_three_nts_counties():
    mig = _load_migration()
    counties = {c for c, _url, _cls in mig._CONNECTORS}
    assert counties == {"pierce", "snohomish", "king"}


def test_seeds_visible_health_status():
    # GET /scrapers/connectors hides unknown/down connectors, so the seed MUST use a
    # visible status ('healthy'/'degraded') or Auction Leads is absent from the picker
    # until a canary samples the rows. DB-backed connector => deterministically healthy.
    mig = _load_migration()
    assert mig._HEALTH_STATUS in ("healthy", "degraded")


def test_every_connector_points_at_a_real_allowlisted_subclass():
    mig = _load_migration()
    for county, base_url, scraper_class in mig._CONNECTORS:
        module_path, class_name = scraper_class.rsplit(".", 1)
        # registry only imports allowlisted modules
        assert module_path in _ALLOWED_SCRAPER_MODULES
        cls = getattr(importlib.import_module(module_path), class_name)
        assert issubclass(cls, _TrusteeSaleScraper)
        # the row's county must match the class it points at
        assert cls.COUNTY == county
        assert base_url.startswith("https://")
