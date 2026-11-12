"""Asotin NTS source (Lewiston Tribune legal notices on nwmarket.com).

Runs against REAL saved pages: the nwmarket sitemap and an advert page (2026-11-12),
and two real Asotin trustee-sale PDFs (MTC 2026-10-09 sale; Aztec, postponed twice).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.sources import nts_nwmarket as nwm
from src.scrapers.sources import nts_pdf
from src.scrapers.sources import nts_tacoma_index as nts

_FIX = Path(__file__).parent / "fixtures"


def _notice(name: str) -> str:
    return nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text((_FIX / name).read_bytes()))


class TestDiscovery:
    def test_window_keeps_only_recent_adverts_on_our_host(self):
        xml = (_FIX / "nts_nwmarket_sitemap.xml").read_text(encoding="utf-8")
        every = nwm.recent_advert_urls(xml, date(2000, 1, 1))
        recent = nwm.recent_advert_urls(xml, date(2026, 11, 10))
        assert len(every) == 266  # 366 advert entries; the live sitemap repeats 100 of them
        assert 0 < len(recent) < len(every)
        assert set(recent) <= set(every)
        assert all(u.startswith("https://www.nwmarket.com/marketplace/advert/-Notices_") for u in every)

    def test_non_advert_and_off_host_locations_are_dropped(self):
        xml = (
            "<url><loc>https://www.nwmarket.com/marketplace/category/legal-notices</loc></url>"
            "<url><loc>https://evil.example/marketplace/advert/-Notices_1</loc></url>"
            "<url><loc>https://www.nwmarket.com/marketplace/advert/-Notices_7</loc>"
            "<lastmod>2026-11-11</lastmod></url>"
        )
        assert nwm.recent_advert_urls(xml, date(2026, 11, 1)) == [
            "https://www.nwmarket.com/marketplace/advert/-Notices_7"]

    def test_advert_page_yields_its_pdf(self):
        html = (_FIX / "nts_nwmarket_advert_2318.html").read_text(encoding="utf-8")
        assert nwm.extract_pdf_url(html) == (
            "https://www.nwmarket.com/marketplace/image/notices/156590/156590_pdf.pdf")

    def test_a_page_without_a_pdf_is_none(self):
        assert nwm.extract_pdf_url("<html><body>Listing no longer available</body></html>") is None


class TestAsotinNotices:
    def test_mtc_notice_parses_every_field(self):
        text = _notice("nts_nwmarket_asotin_155936.pdf")
        assert nwm.is_asotin_property(text)
        p = nts.parse_tacoma_notice(text)
        assert p["ts_number"] == "WA01000007-26-1"
        assert p["parcel"] == "10040300800020000"  # 17 digits, kept whole
        assert p["auction_date"] == "October 9, 2026"
        assert p["property_address"] == "909 WALK LANE, CLARKSTON, WA 99403"
        assert p["principal_owing"] == Decimal("303723.49")
        assert p["trustee"] == "MTC Financial Inc. dba Trustee Corps"

    def test_aztec_notice_takes_the_last_postponement_and_a_clean_address(self):
        text = _notice("nts_nwmarket_asotin_155674.pdf")
        assert nwm.is_asotin_property(text)
        p = nts.parse_tacoma_notice(text)
        # Sale sentence says July 10; continued to Aug 14, then to Sept 11.
        assert (p["auction_date"], p["auction_time"]) == ("September 11, 2026", "10:00 AM")
        assert p["property_address"] == "1104 8th Street, Clarkston, WA 99403"
        assert p["ts_number"] == "REF-366189"
        assert p["principal_owing"] == Decimal("227084.99")
        assert nts.is_valid_nts(p)

    def test_rows_are_tagged_asotin_and_this_source(self):
        p = nts.parse_tacoma_notice(_notice("nts_nwmarket_asotin_155936.pdf"))
        row = nts.notice_to_row(p, source_url="https://www.nwmarket.com/x.pdf", today=date(2026, 9, 1),
                                source=nwm.SOURCE, county=nwm.COUNTY)
        assert row is not None and (row["source"], row["county"]) == ("lmtribune_nwmarket", "asotin")

    def test_another_countys_notice_is_not_asotin(self):
        king = (_FIX / "nts_king_postponed_guiler.txt").read_text(encoding="utf-8")
        assert not nwm.is_asotin_property(king)

    @pytest.mark.parametrize("text", [
        # A sale held at the Asotin courthouse, or a court in Asotin, is not the property.
        "will sell at main stairs Asotin County Courthouse the real property situated in "
        "the County of Garfield, State of Washington",
        "SUPERIOR COURT OF WASHINGTON FOR THE COUNTY OF ASOTIN In re the Estate of",
    ])
    def test_asotin_venue_or_court_alone_is_not_an_asotin_property(self, text):
        assert not nwm.is_asotin_property(text)


def test_the_crawl_task_is_registered_and_scheduled():
    import src.workers.nts_crawler  # noqa: F401 — import registers the @app.task crawlers
    import src.workers.scheduler  # noqa: F401 — import installs the beat schedule
    from src.workers import app

    assert "src.workers.nts_crawler.crawl_nts_nwmarket_asotin" in app.tasks
    tasks = {v["task"] for v in app.conf.beat_schedule.values()}
    assert "src.workers.nts_crawler.crawl_nts_nwmarket_asotin" in tasks
