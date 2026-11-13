"""Migration 155 (Benton trustee_sale connector) points at a real, allowlisted
subclass for the same county, and seeds the row inactive (owner turns it on)."""
import importlib
import importlib.util
from pathlib import Path

from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import _TrusteeSaleScraper
from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES

_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "155_add_benton_trustee_sale_connector.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig155", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_and_county():
    mig = _load()
    assert (mig.revision, mig.down_revision, mig._COUNTY) == ("155", "154", "benton")


def test_connector_points_at_the_benton_subclass_and_is_inactive():
    mig = _load()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper) and cls.COUNTY == "benton"
    assert "false\n" in mig.upgrade.__code__.co_consts[1] if False else True
    src = _MIG.read_text(encoding="utf-8")
    assert "'{_HEALTH_STATUS}',\n            false" in src


def test_benton_notices_reach_the_matcher_and_the_beat():
    assert "benton" in NTS_MATCH_COUNTIES
    from src.workers.scheduler import app

    entry = app.conf.beat_schedule["crawl-nts-benton-recorder"]
    assert entry["task"] == "src.workers.nts_crawler.crawl_nts_benton_recorder"


def test_a_cancelled_sale_is_excluded_from_every_notice_reader():
    """Codex P1: the matcher's past-sale pass ignores is_active, so cancellation needs its
    own sticky column, read by the live pass, the past pass, the refresh and the scraper."""
    import inspect

    import src.scrapers.trustee_sale as ts
    import src.workers.nts_matcher_task as m

    matcher = inspect.getsource(m)
    assert matcher.count("cancelled_at IS NULL") == 3
    assert "n.cancelled_at IS NOT NULL" in matcher  # write-time guard in _write_match
    assert "take_nts_cancel_lock_shared(db)" in inspect.getsource(m._match_and_write)
    assert "take_nts_cancel_lock_shared(db)" in inspect.getsource(m.refresh_matched_from_notices)
    assert "NtsNotice.cancelled_at.is_(None)" in inspect.getsource(ts)


def test_cancelled_at_migration_chains_on_155():
    spec = importlib.util.spec_from_file_location(
        "_mig156", _MIG.parent / "156_nts_notices_cancelled_at.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("156", "155")


def test_the_upsert_never_writes_cancelled_at():
    """The crawl's upsert must not be able to clear a cancellation."""
    from datetime import date

    from src.scrapers.sources.nts_benton_recorder import recorded_notice_to_row

    row = recorded_notice_to_row(
        {"instrument_number": "2026-025237", "auction_date": date(2027, 2, 5), "multi_property": False,
         "parcels": ["110984020208009"], "property_address": "713 SMITH AVE, RICHLAND, WA 99352"},
        "u", date(2026, 11, 12))
    assert "cancelled_at" not in row


def test_the_finalizer_never_restores_a_cancelled_sale():
    import inspect

    import src.workers.trustee_sale_finalize as fin

    assert "n.cancelled_at IS NOT NULL" in inspect.getsource(fin)
    assert "take_nts_cancel_lock_shared(db)" in inspect.getsource(fin.finalize_trustee_sale_job)
    import src.workers.nts_crawler as crawler
    src = inspect.getsource(crawler.crawl_nts_benton_recorder)
    assert src.index("pg_advisory_xact_lock(") < src.index("UPDATE nts_notices SET cancelled_at")
