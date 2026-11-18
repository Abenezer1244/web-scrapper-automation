"""Douglas auction leads: migration 171 wiring, and the crawl task end to end on the test
DB with only the portal replaced (the recorder's detail pages, derived from the live
3286111 page in tests/fixtures/douglas_nts)."""
import importlib
import importlib.util
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.templates.acclaimweb import DouglasAcclaimWebScraper
from src.scrapers.trustee_sale import _TrusteeSaleScraper
from src.workers import nts_crawler
from src.workers.nts_matcher_task import NTS_MATCH_COUNTIES

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "alembic" / "versions" / "171_add_douglas_trustee_sale_connector.py"
_NTS = (_ROOT / "tests" / "fixtures" / "douglas_nts" / "detail_nts_3286111.html").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("_mig171", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_and_county():
    mig = _load()
    assert (mig.revision, mig.down_revision, mig._COUNTY) == ("171", "170", "douglas")


def test_connector_points_at_the_douglas_reader_and_is_inactive():
    mig = _load()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper) and cls.COUNTY == "douglas"
    assert "'{_HEALTH_STATUS}',\n            false" in _MIG.read_text(encoding="utf-8")


def test_douglas_notices_are_crawled_but_not_matched_until_activation():
    # Stored automatically, so they must not stamp customers' pre-foreclosure leads
    # before the owner switches the connector on (Codex): added at activation.
    assert "douglas" not in NTS_MATCH_COUNTIES
    from src.workers.scheduler import app

    entry = app.conf.beat_schedule["crawl-nts-douglas-recorder"]
    assert entry["task"] == "src.workers.nts_crawler.crawl_nts_douglas_recorder"


def _variant(*, instrument: str, doc_type: str | None = None, remarks: str | None = None,
             related: str | None = None) -> str:
    html = _NTS.replace("3286111", instrument)
    if doc_type:
        html = html.replace("NTS - NOTICE OF TRUSTEE SALE", doc_type)
    if remarks is not None:
        start = html.index("NOTICE IS HEREBY GIVEN")
        end = html.index("<div", start)
        html = html[:start] + remarks + " " + html[end:]
    if related:
        html = html.replace("3227557 [DT]", related)
    return html


def _doc(code: str, html: str, item: str, instrument: str = "3286111") -> dict:
    """What the grid listed for this recording (instrument and date checked against the page)."""
    return {"code": code, "item_id": item, "instrument": instrument, "record_date": "11/06/2026", "html": html}


@pytest.fixture
def crawl(monkeypatch):
    """Run the real task with the portal answering ``docs``; alerts captured; the
    douglas_recorder rows removed afterwards (the task writes through its own session)."""
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(nts_crawler, "_alert_recorder",
                        lambda what, detail, county="chelan": alerts.append((what, detail)))

    def run(docs):
        async def fake(self, date_from, date_to, descriptions, readable):
            return docs
        monkeypatch.setattr(DouglasAcclaimWebScraper, "collect_by_doc_type", fake)
        return nts_crawler.crawl_nts_douglas_recorder.run()

    yield run, alerts
    with system_sync_session() as s:
        s.execute(text("DELETE FROM nts_notices WHERE source = 'douglas_recorder'"))
        s.commit()


def _notices() -> dict:
    with system_sync_session() as s:
        rows = s.execute(text("SELECT ts_number, parcel, auction_date, is_active, cancelled_at "
                              "FROM nts_notices WHERE source = 'douglas_recorder'")).all()
    return {r.ts_number: r for r in rows}


def test_a_notice_is_stored_with_its_sale_and_parcel(crawl):
    run, alerts = crawl
    summary = run([_doc("NTS", _NTS, "638916")])
    got = _notices()["REC-3286111"]
    assert (got.parcel, got.auction_date) == ("96600001800", date(2027, 3, 19))
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    assert got.is_active is (date(2027, 3, 19) >= today)
    assert summary["upserted"] == 1 and not alerts


def test_a_discontinuance_in_the_window_cancels_its_notice(crawl):
    run, _ = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3286111 [NTS]")
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    got = _notices()["REC-3286111"]
    assert got.is_active is False and got.cancelled_at is not None
    assert summary["cancelled"] == 1


def test_a_discontinuance_never_cancels_through_its_deed_of_trust_link():
    from src.scrapers.sources import nts_douglas_recorder as rec

    assert rec.discontinued([{"related": [("3227557", "DT"), ("3286111", None), ("99", "MOAG")]}]) \
        == frozenset({"3286111"})


def test_a_blank_related_link_is_unreadable_not_absent():
    from src.scrapers.sources import nts_douglas_recorder as rec

    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3286111 [NTS]</a><a href='#'>")   # a readable link and a blank one
    p = rec.parse_detail(ndts)
    assert (None, "UNREADABLE") in p["related"]
    with pytest.raises(ValueError, match="unreadable"):
        rec.discontinued([p])


@pytest.mark.parametrize("related", ["3227557 [DT]", "3286111 [NTS"])
def test_a_discontinuance_that_names_no_notice_writes_nothing(crawl, related):
    # Only a deed-of-trust link, or a label that does not read whole: which sale it ends
    # is unknown, so no notice may go live this run (Codex).
    run, alerts = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related=related)
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    assert _notices() == {}
    assert summary.get("error") and "nothing written" in alerts[0][0]


def test_a_detail_page_of_another_recording_writes_nothing(crawl):
    run, alerts = crawl
    summary = run([_doc("NTS", _NTS, "638916", instrument="9999999")])  # grid said another instrument
    assert _notices() == {} and summary.get("error")


def test_a_held_notice_is_not_stored_and_alerts(crawl):
    run, alerts = crawl
    held = _variant(instrument="3286112", remarks="FEBRUARY 5, 2027 FRONT ENTRANCE OF DOUGLAS COUNTY")
    summary = run([_doc("NTS", held, "638917", instrument="3286112")])
    assert _notices() == {}
    assert summary["held"] == {"no_sale_language": 1}
    assert alerts and "held" in alerts[0][0]


def test_a_published_notice_that_now_reads_as_held_is_taken_down_not_cancelled(crawl):
    run, _ = crawl
    run([_doc("NTS", _NTS, "638916")])
    edited = _variant(instrument="3286111", remarks="SALE POSTPONED, SEE TRUSTEE")
    summary = run([_doc("NTS", edited, "638916")])
    got = _notices()["REC-3286111"]
    assert got.is_active is False and got.cancelled_at is None   # comes back if it parses again
    assert summary["held"] == {"no_date": 1}


def test_a_page_that_does_not_parse_writes_nothing(crawl):
    run, alerts = crawl
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", "<html>Error 5001</html>", "640000")])
    assert _notices() == {}                      # the notice is NOT stored without its discontinuance
    assert summary.get("error") and alerts and "nothing written" in alerts[0][0]
