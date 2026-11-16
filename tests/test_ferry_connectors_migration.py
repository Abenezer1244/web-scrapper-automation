"""Migration 164 (Ferry connectors) — the seed data must be valid and stay switched off.

No DB: loads the migration and checks every row points at a real, allowlisted scraper
class, the tax source is trusted (and, like Asotin's, not cap-exempt), the court rows'
base_url is the host the Archives reader talks to, and nothing is seeded active.
"""
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from urllib.parse import urlparse

from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.sources import wa_digital_archives as da
from src.workers.tasks_helpers.dedup import _TRUSTED_TAX_SOURCES

_MIG_PATH = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
             / "164_add_ferry_connectors.py")


def _load():
    spec = importlib.util.spec_from_file_location("_mig164", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chains_onto_163():
    mig = _load()
    assert (mig.revision, mig.down_revision) == ("164", "163")


def test_every_row_is_a_real_allowlisted_scraper_for_its_record_type():
    mig = _load()
    types = set()
    for _, record_types, scraper_class, _, _ in mig._ROWS:
        module_path, cls_name = scraper_class.rsplit(".", 1)
        assert module_path in _ALLOWED_SCRAPER_MODULES
        cls = getattr(importlib.import_module(module_path), cls_name)
        (record_type,) = json.loads(record_types)
        assert getattr(cls, "RECORD_TYPE", record_type) == record_type
        types.add(record_type)
    assert types == {"tax_delinquent", "probate", "divorce"}


def test_the_tax_source_is_trusted_and_like_asotin_not_cap_exempt():
    from src.scrapers.ferry_wa_tax_delinquent import SOURCE

    assert SOURCE in _TRUSTED_TAX_SOURCES and SOURCE not in TAX_CAP_EXEMPT_SOURCES


def test_court_rows_register_the_host_the_archives_reader_uses():
    mig = _load()
    assert urlparse(mig._ARCHIVES).netloc == urlparse(da.ORIGIN).netloc


def test_nothing_is_seeded_active():
    src = inspect.getsource(_load().upgrade)
    assert "'unknown', false" in src and "true" not in src.lower()
