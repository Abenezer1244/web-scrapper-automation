"""NTS-backed lists must not report "0 leads" when the source was never read.

trustee_sale (Auction Leads) reads a cache the crawler fills, and Snohomish
pre_foreclosure reads the same weekly Tribune PDF directly. Both used to finish a
job DONE with 0 leads when the crawler was dead, the extraction was empty, or every
notice failed to parse. Real DB and real fixture PDFs; no mocks except the one
network failure that cannot be produced on demand.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from sqlalchemy import text

from src.scrapers import snohomish_wa_pre_foreclosure as snoho
from src.scrapers.enrichment.source_health import get_source_state, sources_due_for_probe
from src.scrapers.reliability import ScraperExecutionError, TransientScrapeError
from src.scrapers.sources import nts_pdf
from src.scrapers.trustee_sale import (
    _TrusteeSaleScraper,
    cache_staleness_reason,
    nts_crawl_heartbeat_key,
)
from src.utils.lead_signals import auction_reference_date
from src.workers.nts_crawler import _record_crawl_heartbeat

_FIXTURES = Path(__file__).parent / "fixtures"
_TEST_COUNTY = "nts_silent_empty_test"
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _issue_text(name: str) -> str:
    return nts_pdf.normalize_pdf_text(
        nts_pdf.extract_pdf_text((_FIXTURES / name).read_bytes())
    )


# ─── cache freshness decision (pure) ─────────────────────────────────────────

class TestCacheStalenessReason:
    def test_recent_heartbeat_is_fresh_even_if_no_notice_was_written_for_weeks(self):
        # A sale-free stretch writes no rows; the heartbeat is what proves the read.
        assert cache_staleness_reason(_NOW - timedelta(days=2), _NOW - timedelta(days=40), _NOW) is None

    def test_old_heartbeat_is_stale(self):
        reason = cache_staleness_reason(_NOW - timedelta(days=4), _NOW, _NOW)
        assert reason and "not read its source" in reason

    def test_without_a_heartbeat_fall_back_to_fetched_at(self):
        assert cache_staleness_reason(None, _NOW - timedelta(days=10), _NOW) is None
        reason = cache_staleness_reason(None, _NOW - timedelta(days=16), _NOW)
        assert reason and "refreshed since" in reason

    def test_never_filled_is_stale(self):
        assert "never been filled" in cache_staleness_reason(None, None, _NOW)

    def test_heartbeat_key_is_normalized(self):
        assert nts_crawl_heartbeat_key(" Snohomish ") == "nts_crawl:snohomish"


# ─── heartbeat row + the scraper against the real cache ──────────────────────

@pytest.fixture
def sync_db():
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as s:
        yield s


@pytest.fixture
def clean_county(sync_db):
    def _wipe():
        sync_db.execute(text("DELETE FROM nts_notices WHERE county = :c"), {"c": _TEST_COUNTY})
        sync_db.execute(
            text("DELETE FROM external_source_health WHERE source_key = :k"),
            {"k": nts_crawl_heartbeat_key(_TEST_COUNTY)},
        )
        sync_db.commit()

    _wipe()
    yield
    _wipe()


class _TestCountyScraper(_TrusteeSaleScraper):
    COUNTY = _TEST_COUNTY


def _scrape():
    return asyncio.run(_TestCountyScraper().scrape("", ""))


def _set_heartbeat(sync_db, when: datetime) -> None:
    sync_db.execute(
        text(
            "INSERT INTO external_source_health (source_key, status, last_probe_at, "
            "last_success_at, consecutive_probe_failures, updated_at) "
            "VALUES (:k, 'healthy', :t, :t, 0, :t)"
        ),
        {"k": nts_crawl_heartbeat_key(_TEST_COUNTY), "t": when},
    )
    sync_db.commit()


def _add_notice(sync_db, *, fetched_at: datetime) -> None:
    sync_db.execute(
        text(
            "INSERT INTO nts_notices (id, source, ts_number, county, state, parcel, "
            "property_address, auction_date, grantor, is_active, fetched_at) VALUES "
            "(gen_random_uuid(), 'nts_silent_empty_src', 'TS-SILENT-1', :c, 'WA', "
            "'00100000000001', '1 TEST ST, EVERETT, WA 98201', :d, 'TEST GRANTOR', true, :f)"
        ),
        {"c": _TEST_COUNTY, "d": auction_reference_date() + timedelta(days=10), "f": fetched_at},
    )
    sync_db.commit()


class TestTrusteeSaleScrape:
    def test_heartbeat_row_is_never_probed_by_the_source_canary(self, sync_db, clean_county):
        _record_crawl_heartbeat(sync_db, _TEST_COUNTY)
        state = get_source_state(sync_db, nts_crawl_heartbeat_key(_TEST_COUNTY))
        assert state["status"] == "healthy" and state["last_success_at"] is not None
        assert nts_crawl_heartbeat_key(_TEST_COUNTY) not in sources_due_for_probe(sync_db)

    def test_empty_cache_that_nobody_refreshes_fails_the_job(self, clean_county):
        with pytest.raises(ScraperExecutionError, match="never been filled"):
            _scrape()

    def test_empty_cache_with_a_recent_crawl_is_a_real_zero(self, sync_db, clean_county):
        _set_heartbeat(sync_db, datetime.now(UTC) - timedelta(hours=6))
        assert _scrape() == []

    def test_empty_cache_with_a_stale_crawl_fails_the_job(self, sync_db, clean_county):
        _set_heartbeat(sync_db, datetime.now(UTC) - timedelta(days=5))
        with pytest.raises(ScraperExecutionError, match="not read its source"):
            _scrape()

    def test_real_upcoming_sales_are_delivered_even_from_a_stale_cache(self, sync_db, clean_county):
        _set_heartbeat(sync_db, datetime.now(UTC) - timedelta(days=5))
        _add_notice(sync_db, fetched_at=datetime.now(UTC) - timedelta(days=5))
        records = _scrape()
        assert [r.parcel_id for r in records] == ["00100000000001"]


# ─── Snohomish pre_foreclosure: what an issue's parse result means (pure) ────

class TestLegalsIssueValidity:
    @pytest.mark.parametrize(
        "name",
        [
            "nts_snoho_tribune_2025-12-17.pdf",
            "nts_snoho_tribune_2026-08-05.pdf",
            "nts_queen_anne_news_2026-06-24.pdf",
            "nts_queen_anne_news_2026-07-01.pdf",
        ],
    )
    def test_real_issues_are_readable(self, name):
        assert nts_pdf.looks_like_legals_issue(_issue_text(name))

    def test_an_issue_with_no_trustee_sale_is_still_a_readable_issue(self):
        # Cut every trustee-sale notice out of a real issue: summonses, probate
        # notices and bids remain, so a sale-free week must not read as a failure.
        text_ = _issue_text("nts_snoho_tribune_2026-08-05.pdf")
        for block in nts_pdf.split_notice_blocks(text_):
            text_ = text_.replace(block, "")
        assert nts_pdf.split_notice_blocks(text_) == []
        assert nts_pdf.looks_like_legals_issue(text_)

    def test_empty_or_tiny_extractions_are_not_issues(self):
        assert not nts_pdf.looks_like_legals_issue("")
        assert not nts_pdf.looks_like_legals_issue("NOTICE " * 50)


class TestAssessNoticeParse:
    def test_unreadable_extraction_raises_instead_of_reporting_zero(self):
        with pytest.raises(ScraperExecutionError, match="not a readable"):
            snoho.assess_notice_parse(0, 0, issue_read=False)

    def test_notices_present_but_none_parsed_raises(self):
        with pytest.raises(ScraperExecutionError, match="none parsed"):
            snoho.assess_notice_parse(10, 0, issue_read=True)

    def test_partial_parse_delivers_and_reports_the_gap(self):
        # Measured on the 9-9-26 Tribune issue: 10 notices, 7 parse.
        assert snoho.assess_notice_parse(10, 7, issue_read=True) == (
            "3 of 10 trustee-sale notices did not parse"
        )

    def test_a_real_issue_without_sales_is_a_genuine_zero(self):
        assert snoho.assess_notice_parse(0, 0, issue_read=True) is None
        assert snoho.assess_notice_parse(8, 8, issue_read=True) is None


class TestDiscoveryFailures:
    def test_network_failure_is_retryable_not_a_layout_change(self, monkeypatch):
        def _unreachable(*_a, **_k):
            raise requests.ConnectionError("simulated outage")

        monkeypatch.setattr(snoho, "safe_get_following", _unreachable)
        with pytest.raises(TransientScrapeError, match="unreachable"):
            snoho._discover_pdf_url()


class _Page:
    """A fetched legal-notices page: the two fields discovery reads."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.text = body


_LEGALS_LINK = (
    '<a href="https://pacificpublishingcompany.media.clients.ellingtoncms.com'
    '/static-4/snoho/images/Legals 9-9-26.pdf">Legals</a>'
)


class TestDiscoveryStatuses:
    def test_the_normal_soft_404_with_a_link_is_accepted(self, monkeypatch):
        monkeypatch.setattr(snoho, "safe_get_following", lambda *a, **k: _Page(404, _LEGALS_LINK))
        assert snoho._discover_pdf_url().endswith("/Legals 9-9-26.pdf")

    @pytest.mark.parametrize("status", [429, 503])
    def test_a_throttled_or_failing_page_is_retried_even_with_a_link(self, monkeypatch, status):
        monkeypatch.setattr(snoho, "safe_get_following", lambda *a, **k: _Page(status, _LEGALS_LINK))
        with pytest.raises(TransientScrapeError, match=f"HTTP {status}"):
            snoho._discover_pdf_url()

    def test_a_non_transport_error_is_not_retried(self, monkeypatch):
        def _blocked(*_a, **_k):
            raise ValueError("Blocked hop")

        monkeypatch.setattr(snoho, "safe_get_following", _blocked)
        with pytest.raises(ValueError, match="Blocked hop"):
            snoho._discover_pdf_url()

    def test_a_pdf_download_timeout_is_retryable(self, monkeypatch):
        monkeypatch.setattr(snoho, "safe_get_following", lambda *a, **k: _Page(404, _LEGALS_LINK))

        def _timeout(*_a, **_k):
            raise requests.Timeout("simulated")

        monkeypatch.setattr(snoho, "safe_download_to_file", _timeout)
        with pytest.raises(TransientScrapeError, match="download failed"):
            asyncio.run(snoho.SnohomishWAPreForeclosureScraper().scrape("", ""))


class TestCrawlerHeartbeat:
    """The real Pacific Publishing crawl path, fed a real issue from disk."""

    def _serve_fixture(self, monkeypatch, name: str):
        import shutil

        import src.utils.safe_http as safe_http
        from src.workers import nts_crawler

        monkeypatch.setattr(
            nts_crawler, "_discover_latest_legals_pdf",
            lambda *_a, **_k: "https://pacificpublishingcompany.media.clients.ellingtoncms.com/x.pdf",
        )
        monkeypatch.setattr(
            safe_http, "safe_download_to_file",
            lambda _url, path, **_k: shutil.copyfile(_FIXTURES / name, path),
        )
        return nts_crawler

    def test_a_crawl_that_read_a_real_issue_records_the_heartbeat(
        self, sync_db, clean_county, monkeypatch
    ):
        crawler = self._serve_fixture(monkeypatch, "nts_snoho_tribune_2026-08-05.pdf")
        summary = crawler._crawl_pacific_publishing_pdf(
            page_url="https://www.snoho.com/", pdf_path_prefix="/static-4/snoho/images/",
            source="nts_silent_empty_src", county=_TEST_COUNTY,
        )
        assert summary["issue_read"] is True
        state = get_source_state(sync_db, nts_crawl_heartbeat_key(_TEST_COUNTY))
        assert state is not None and state["last_success_at"] is not None

    def test_a_crawl_that_found_no_pdf_records_no_heartbeat(self, sync_db, clean_county, monkeypatch):
        from src.workers import nts_crawler

        monkeypatch.setattr(nts_crawler, "_discover_latest_legals_pdf", lambda *_a, **_k: None)
        summary = nts_crawler._crawl_pacific_publishing_pdf(
            page_url="https://www.snoho.com/", pdf_path_prefix="/static-4/snoho/images/",
            source="nts_silent_empty_src", county=_TEST_COUNTY,
        )
        assert summary["issue_read"] is False
        assert get_source_state(sync_db, nts_crawl_heartbeat_key(_TEST_COUNTY)) is None

    def test_a_real_issue_whose_notices_all_fail_to_parse_records_no_heartbeat(
        self, sync_db, clean_county, monkeypatch
    ):
        # Parser drift: the issue is readable and has trustee-sale blocks, but none
        # parse. A heartbeat here would make an empty cache look fresh.
        crawler = self._serve_fixture(monkeypatch, "nts_snoho_tribune_2026-08-05.pdf")
        summary = crawler._crawl_pacific_publishing_pdf(
            page_url="https://www.snoho.com/", pdf_path_prefix="/static-4/snoho/images/",
            source="nts_silent_empty_src", county=_TEST_COUNTY,
            parse_fn=lambda _block: {},
        )
        assert summary["issue_read"] is True and summary["blocks"] > 0
        assert summary["heartbeat"] is False
        assert get_source_state(sync_db, nts_crawl_heartbeat_key(_TEST_COUNTY)) is None
        stored = sync_db.execute(
            text("SELECT count(*) FROM nts_notices WHERE county = :c"), {"c": _TEST_COUNTY}
        ).scalar_one()
        assert stored == 0
