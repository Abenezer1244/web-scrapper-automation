"""Migration 140 (Clallam death_certificate + tax connector) — the seed data must be valid.

No DB: loads the migration and checks that its scraper class is a real, allowlisted
scraper, that the recorder types it adds are ones the EagleWeb template serves, and that
the tax source it enables is trusted and cap-exempt (owner decision 2026-11-12).
"""
import importlib
import importlib.util
import json
from pathlib import Path

from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.templates.eagleweb import _DOC_TYPE_MAP
from src.workers.tasks_helpers.dedup import _TRUSTED_TAX_SOURCES

_MIG_PATH = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
             / "140_clallam_death_certificate_and_tax.py")


def _load():
    spec = importlib.util.spec_from_file_location("_mig140", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chains_onto_139():
    mig = _load()
    assert (mig.revision, mig.down_revision) == ("140", "139")


def test_recorder_types_only_add_death_certificate_and_the_template_serves_them():
    mig = _load()
    old, new = json.loads(mig._OLD_TYPES), json.loads(mig._NEW_TYPES)
    assert set(new) - set(old) == {"death_certificate"}
    assert all(t in _DOC_TYPE_MAP for t in new)


def test_tax_connector_points_at_a_real_allowlisted_scraper_with_a_trusted_exempt_source():
    mig = _load()
    module_path, cls_name = mig._TAX_SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    mod = importlib.import_module(module_path)
    assert hasattr(mod, cls_name)
    assert mod._SOURCE in _TRUSTED_TAX_SOURCES
    assert mod._SOURCE in TAX_CAP_EXEMPT_SOURCES
    assert mig._TAX_BASE_URL == mod._LANDING_URL
