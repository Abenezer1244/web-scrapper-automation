"""Chelan trustee_sale delivery (4a-3b): migration 159's connector, the scraper subclass,
the matcher and beat wiring, the OCR binary in the image, and when the crawl may call
Chelan's notice cache fresh."""
import importlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import ChelanWATrusteeSaleScraper, _TrusteeSaleScraper
from src.workers.nts_crawler import (
    RECORDER_REVIEW_LAG_DAYS,
    chelan_cache_is_fresh,
    chelan_unresolved_reason,
)
from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "alembic" / "versions" / "159_add_chelan_trustee_sale_connector.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig159", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_migration_chains_on_the_staging_table():
    mig = _load()
    assert (mig.revision, mig.down_revision, mig._COUNTY) == ("159", "158", "chelan")


def test_the_connector_points_at_the_chelan_subclass_and_ships_switched_off():
    mig = _load()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert cls is ChelanWATrusteeSaleScraper and issubclass(cls, _TrusteeSaleScraper)
    assert cls.COUNTY == "chelan"
    # Off until discontinuances are reconciled (4a-3c): never delivered before that.
    assert "'{_HEALTH_STATUS}',\n            false" in _MIG.read_text(encoding="utf-8")


def test_the_scope_says_recorded_and_reviewed_not_newspaper():
    scope = ChelanWATrusteeSaleScraper.collection_scope("trustee_sale")
    assert "Chelan County Auditor's recorded documents" in scope.note and "reviewed" in scope.note
    assert ChelanWATrusteeSaleScraper.collection_scope("probate") is None


def test_chelan_notices_reach_the_matcher_and_the_crawl_runs_daily_before_it():
    # Added with discontinuance reconciliation (4a-3c), not before (Codex High, 4a-3b).
    assert "chelan" in NTS_MATCH_COUNTIES
    from src.workers.scheduler import app

    crawl = app.conf.beat_schedule["crawl-nts-chelan-recorder"]
    assert crawl["task"] == "src.workers.nts_crawler.crawl_nts_chelan_recorder"
    # Its 50-minute limit ends before the 11:00 UTC matcher.
    assert crawl["schedule"].hour == {9} and crawl["schedule"].minute == {50}


def test_the_image_installs_tesseract():
    assert "    tesseract-ocr \\\n" in (_ROOT / "Dockerfile").read_text(encoding="utf-8")


_CLEAN = {"discovered": 3, "staged": 1, "kept": 2, "failed": 0, "invalid": 0, "unread": 0}


@pytest.mark.parametrize("summary,fresh", [
    (_CLEAN, True),
    ({**_CLEAN, "discovered": 0, "staged": 0, "kept": 0}, True),   # a verified quiet month
    ({**_CLEAN, "failed": 1}, False),
    ({**_CLEAN, "unread": 2}, False),
    ({**_CLEAN, "invalid": 1}, False),
    ({**_CLEAN, "error": "discovery failed"}, False),
    ({**_CLEAN, "skipped": "locked"}, False),
], ids=["clean", "verified-zero", "failed", "unread", "invalid", "discovery-error", "locked"])
def test_the_crawl_heartbeat_needs_a_complete_read(summary, fresh):
    assert chelan_cache_is_fresh(summary) is fresh


def test_the_crawl_records_its_heartbeat_only_through_the_freshness_rule():
    import inspect

    import src.workers.nts_crawler as crawler

    src = inspect.getsource(crawler.crawl_nts_chelan_recorder)
    assert src.count("_record_crawl_heartbeat(") == 1
    assert src.index("chelan_cache_is_fresh(") < src.index("_record_crawl_heartbeat(")


# ── unresolved recordings, judged at delivery time (real test database) ──

@pytest.fixture
def staging():
    from sqlalchemy import text

    from src.db.session import system_sync_session

    def _clean(db):
        db.execute(text("DELETE FROM nts_recorder_documents WHERE county = 'chelan'"))
        db.execute(text("DELETE FROM nts_notices WHERE county = 'chelan'"))
        db.execute(text("DELETE FROM external_source_health WHERE source_key = 'nts_crawl:chelan'"))
        db.commit()

    with system_sync_session() as db:
        _clean(db)
        yield db
        db.rollback()
        _clean(db)


def _row(db, item: str, status: str, age_days: float, record_date: str = "2026-05-01") -> None:
    from sqlalchemy import text

    db.execute(text(
        "INSERT INTO nts_recorder_documents (id, county, state, transaction_item_id, instrument_number, "
        "doc_code, record_date, status, decision_note, reviewed_by, reviewed_at, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'chelan', 'WA', :i, :i, 'NTS', :rd, :s, :n, :rb, :ra, "
        "now() - make_interval(secs => :age), now())"),
        {"i": item, "rd": record_date, "s": status, "age": age_days * 86400,
         "n": None if status == "pending_review" else "x",
         "rb": "op" if status in ("approved", "rejected", "superseded") else None,
         "ra": datetime.now(UTC) if status in ("approved", "rejected", "superseded") else None})
    db.commit()


@pytest.mark.parametrize("status,age,blocks", [
    ("pending_review", RECORDER_REVIEW_LAG_DAYS - 0.5, False),   # still within its review time
    ("pending_review", RECORDER_REVIEW_LAG_DAYS + 0.5, True),
    ("failed", RECORDER_REVIEW_LAG_DAYS + 0.5, True),            # unreadable, never resolved
    ("failed", 40, True),                                         # expired unreviewed, out of window
    ("rejected", 40, False),
], ids=["pending-on-time", "pending-overdue", "failed", "expired-outside-window", "rejected"])
def test_an_unresolved_recording_makes_the_cache_short_wherever_it_sits(staging, status, age, blocks):
    _row(staging, "9800001", status, age)
    reason = chelan_unresolved_reason(staging, datetime.now(UTC))
    assert (reason is not None) is blocks
    if blocks:
        assert "scripts/nts_review.py list" in reason


def test_the_scraper_refuses_an_empty_list_while_a_review_is_overdue_despite_a_fresh_crawl(staging):
    import asyncio

    from src.scrapers.enrichment.source_health import mark_source_healthy
    from src.scrapers.reliability import ScraperExecutionError

    mark_source_healthy(staging, "nts_crawl:chelan")   # the crawl read its source an instant ago
    scraper = ChelanWATrusteeSaleScraper()
    assert asyncio.run(scraper.scrape("01/01/2026", "03/31/2026")) == []   # control: verified zero
    _row(staging, "9800002", "pending_review", RECORDER_REVIEW_LAG_DAYS + 1)
    with pytest.raises(ScraperExecutionError, match="unresolved since"):
        asyncio.run(scraper.scrape("01/01/2026", "03/31/2026"))
