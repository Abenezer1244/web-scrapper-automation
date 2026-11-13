"""Migration 150 (Asotin trustee_sale connector) — valid seed, and seeded INACTIVE.

No DB: loads the migration module and checks its row points at a real, allowlisted
subclass for the same county, and that it stays hidden until the release is approved.
"""
import importlib
import importlib.util
from pathlib import Path

from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import _TrusteeSaleScraper
from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES

_MIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "150_add_asotin_trustee_sale_connector.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig150", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chains_onto_current_head():
    mig = _load_migration()
    assert (mig.revision, mig.down_revision) == ("150", "139")


def test_connector_points_at_a_real_allowlisted_asotin_subclass():
    mig = _load_migration()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper)
    assert cls.COUNTY == mig._COUNTY == "asotin"
    assert mig._ASSESSOR_URL.startswith("https://propertysearch.trueautomation.com/")


def test_row_is_seeded_inactive():
    # The public coverage count must not move until Asotin is approved for release.
    source = _MIG_PATH.read_text(encoding="utf-8")
    values = source.split("SELECT", 1)[1].split("WHERE NOT EXISTS", 1)[0]
    assert values.strip().rstrip().endswith("false")


def test_asotin_notices_are_matched_to_leads():
    assert "asotin" in NTS_MATCH_COUNTIES
