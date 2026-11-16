"""Cowlitz auction leads (3c): migration 166 seeds an INACTIVE connector at the Cowlitz
reader; approved notices reach the matcher; the daily crawl is scheduled; and a notice
still awaiting review holds delivery as a source gap (as Chelan)."""
import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import CowlitzWATrusteeSaleScraper, _TrusteeSaleScraper
from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES
from tests.test_nts_cowlitz_recorder import _stage, db  # noqa: F401 -- rolled-back DB fixture
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "166_add_cowlitz_trustee_sale_connector.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig166", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_and_county():
    mig = _load()
    assert (mig.revision, mig.down_revision, mig._COUNTY) == ("166", "165", "cowlitz")


def test_connector_points_at_the_cowlitz_reader_and_is_inactive():
    mig = _load()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper) and cls.COUNTY == "cowlitz"
    assert "'{_HEALTH_STATUS}',\n            false" in _MIG.read_text(encoding="utf-8")


def test_cowlitz_notices_reach_the_matcher_and_the_beat():
    assert "cowlitz" in NTS_MATCH_COUNTIES
    from src.workers.scheduler import app

    crawl = app.conf.beat_schedule["crawl-nts-cowlitz-recorder"]
    assert crawl["task"] == "src.workers.nts_crawler.crawl_nts_cowlitz_recorder"
    chelan = app.conf.beat_schedule["crawl-nts-chelan-recorder"]["schedule"]
    at = (min(crawl["schedule"].hour), min(crawl["schedule"].minute))  # crontab fields are sets
    assert at < (min(chelan.hour), min(chelan.minute))   # finishes before Chelan's OCR run


def test_a_notice_awaiting_review_is_a_source_gap_at_delivery(db, tmp_path):  # noqa: F811
    from src.db.models import NtsRecorderDocument as Doc

    doc = db.get(Doc, _stage(db, tmp_path))
    doc.created_at = datetime.now(UTC) - timedelta(days=5)   # past the review lag
    db.flush()
    s = CowlitzWATrusteeSaleScraper.__new__(CowlitzWATrusteeSaleScraper)
    gap = s._source_gap(db, datetime.now(UTC))
    assert gap and "unresolved since" in gap


def test_the_scraper_refuses_an_empty_list_while_a_cowlitz_review_is_overdue():
    """Delivery, not just the gap: as Chelan, an overdue unreviewed notice blocks an
    empty answer even with a fresh crawl; within the review lag it does not."""
    import asyncio

    import pytest
    from sqlalchemy import text

    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import mark_source_healthy
    from src.scrapers.reliability import ScraperExecutionError
    from src.workers.nts_crawler import RECORDER_REVIEW_LAG_DAYS

    def clean(s):
        s.execute(text("DELETE FROM nts_recorder_documents WHERE county = 'cowlitz'"))
        s.execute(text("DELETE FROM external_source_health WHERE source_key = 'nts_crawl:cowlitz'"))
        s.commit()

    def row(s, item, age_days):
        s.execute(text(
            "INSERT INTO nts_recorder_documents (id, county, state, transaction_item_id, instrument_number, "
            "doc_code, record_date, status, created_at, updated_at) VALUES (gen_random_uuid(), 'cowlitz', 'WA', "
            ":i, :i, 'NTTRUS', '2026-05-01', 'pending_review', now() - make_interval(secs => :a), now())"),
            {"i": item, "a": age_days * 86400})
        s.commit()

    with system_sync_session() as s:
        clean(s)
        try:
            mark_source_healthy(s, "nts_crawl:cowlitz")
            scraper = CowlitzWATrusteeSaleScraper()
            row(s, "9800001", RECORDER_REVIEW_LAG_DAYS - 0.5)          # within its review time
            assert asyncio.run(scraper.scrape("01/01/2026", "03/31/2026")) == []
            row(s, "9800002", RECORDER_REVIEW_LAG_DAYS + 1)            # overdue
            with pytest.raises(ScraperExecutionError, match="unresolved since"):
                asyncio.run(scraper.scrape("01/01/2026", "03/31/2026"))
        finally:
            s.rollback()
            clean(s)
