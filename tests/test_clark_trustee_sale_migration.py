"""Migration 082 (Clark trustee_sale connector) — the seed data must be valid.

No DB: loads the migration module and asserts its connector row points at a REAL,
allowlisted scraper subclass whose COUNTY matches the row's county — catching a
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
    / "alembic" / "versions" / "082_add_clark_trustee_sale_connector.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig082", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chains_onto_current_head():
    mig = _load_migration()
    assert mig.revision == "082"
    assert mig.down_revision == "081"


def test_seeds_clark():
    mig = _load_migration()
    assert mig._COUNTY == "clark"


def test_seeds_visible_health_status():
    # GET /scrapers/connectors hides unknown/down connectors — a DB-backed connector is
    # deterministically healthy whenever the cache has data, so seed a visible status.
    mig = _load_migration()
    assert mig._HEALTH_STATUS in ("healthy", "degraded")


def test_connector_points_at_a_real_allowlisted_subclass():
    mig = _load_migration()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES  # registry only imports allowlisted
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper)
    assert cls.COUNTY == mig._COUNTY  # the row's county matches the class it points at
    assert mig._BASE_URL.startswith("https://")
