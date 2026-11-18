"""Douglas auction leads: migration 172 wiring, and the crawl task end to end on the test
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
_MIG = _ROOT / "alembic" / "versions" / "172_add_douglas_trustee_sale_connector.py"
_NTS = (_ROOT / "tests" / "fixtures" / "douglas_nts" / "detail_nts_3286111.html").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("_mig172", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chain_and_county():
    mig = _load()
    assert (mig.revision, mig.down_revision, mig._COUNTY) == ("172", "171", "douglas")


def test_connector_points_at_the_douglas_reader_and_is_inactive():
    mig = _load()
    module_path, class_name = mig._SCRAPER_CLASS.rsplit(".", 1)
    assert module_path in _ALLOWED_SCRAPER_MODULES
    cls = getattr(importlib.import_module(module_path), class_name)
    assert issubclass(cls, _TrusteeSaleScraper) and cls.COUNTY == "douglas"
    assert "'{_HEALTH_STATUS}',\n            false" in _MIG.read_text(encoding="utf-8")


def test_douglas_notices_are_crawled_but_not_matched_until_the_first_crawl_is_checked():
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
                        lambda what, detail, county="chelan", topic="": alerts.append((what, detail)))

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

    ended, inferred, unmatched = rec.discontinued(
        [{"instrument": "3290000", "related": [("3227557", "DT"), ("3286111", None), ("99", "MOAG")]}])
    assert (ended, inferred, unmatched) == (frozenset({"3286111"}), frozenset(), [])


def test_a_blank_related_link_is_unreadable_not_absent():
    from src.scrapers.sources import nts_douglas_recorder as rec

    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3286111 [NTS]</a><a href='#'>")   # a readable link and a blank one
    p = rec.parse_detail(ndts)
    assert (None, "UNREADABLE") in p["related"]
    with pytest.raises(ValueError, match="unreadable"):
        rec.discontinued([p])


def test_a_discontinuance_link_that_does_not_read_whole_writes_nothing(crawl):
    run, alerts = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3286111 [NTS")
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    assert _notices() == {}
    assert summary.get("error") and "nothing written" in alerts[0][0]


def test_a_deed_of_trust_only_discontinuance_ends_the_earlier_notice_never_a_later_one():
    """Live: discontinuance 3279193 (11/2025) links only the deed of trust that the live
    notice 3286111 (11/2026) also names: a later foreclosure on the same loan."""
    from datetime import date as d_

    from src.scrapers.sources import nts_douglas_recorder as rec

    def notice(inst, day):
        return {"instrument": inst, "record_date": day, "related": [("3227557", "DT")], "apns": ["96600001800"]}

    earlier, later = notice("3279000", d_(2025, 11, 1)), notice("3286111", d_(2026, 11, 6))
    ndts = {"instrument": "3279193", "record_date": d_(2025, 11, 26),
            "related": [("3227557", "DT"), ("3200000", "MOAG")], "apns": ["96600001800"]}
    # Inferred (taken down, not cancelled for good); the later foreclosure stays live.
    assert rec.discontinued([ndts], [earlier, later]) == (frozenset(), frozenset({"3279000"}), [])


def test_a_deed_linked_discontinuance_also_ends_an_earlier_notice_on_its_parcel():
    # Codex: an earlier notice whose [DT] link is missing still shares the parcel.
    from datetime import date as d_

    from src.scrapers.sources import nts_douglas_recorder as rec

    no_dt = {"instrument": "3279000", "record_date": d_(2025, 11, 1), "related": [], "apns": ["966-000-01800"]}
    ndts = {"instrument": "3279193", "record_date": d_(2025, 11, 26), "related": [("3227557", "DT")],
            "apns": ["96600001800"]}
    assert rec.discontinued([ndts], [no_dt])[1] == frozenset({"3279000"})   # hyphens normalised


def test_two_loans_on_one_parcel_are_two_foreclosures():
    from datetime import date as d_

    from src.scrapers.sources import nts_douglas_recorder as rec

    other_loan = {"instrument": "3279000", "record_date": d_(2025, 11, 1),
                  "related": [("3111111", "DT")], "apns": ["96600001800"]}
    ndts = {"instrument": "3279193", "record_date": d_(2025, 11, 26), "related": [("3227557", "DT")],
            "apns": ["96600001800"]}
    assert rec.discontinued([ndts], [other_loan]) == (frozenset(), frozenset(), ["3279193"])


def test_a_discontinuance_with_neither_notice_nor_deed_link_falls_back_to_the_parcel():
    from datetime import date as d_

    from src.scrapers.sources import nts_douglas_recorder as rec

    before = {"instrument": "3279000", "record_date": d_(2025, 11, 1), "related": [], "apns": ["96600001800"]}
    other = {"instrument": "3279001", "record_date": d_(2025, 11, 1), "related": [], "apns": ["11111111111"]}
    ndts = {"instrument": "3279305", "record_date": d_(2025, 12, 5), "related": [("3200000", "MOAG")],
            "apns": ["96600001800"]}
    assert rec.discontinued([ndts], [before, other]) == (frozenset(), frozenset({"3279000"}), [])


def test_a_discontinuance_that_matches_nothing_is_reported_not_fatal(crawl):
    run, alerts = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3100000 [DT]")
    ndts = ndts.replace("96600001800", "12345678901")       # another parcel, another loan
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    assert _notices()["REC-3286111"].cancelled_at is None
    assert summary["unmatched_discontinuances"] == ["3290000"]
    assert any("matched no notice" in what for what, _ in alerts)


def test_an_inferred_end_holds_across_runs_and_lifts_when_it_no_longer_applies(crawl):
    run, _ = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3227557 [DT]")      # the notice's own deed of trust, no notice link
    docs = [_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")]
    run([_doc("NTS", _NTS, "638916")])           # published first
    assert "REC-3286111" in _notices()
    summary = run(docs)
    assert summary["inferred_ends"] == 1
    for _ in range(2):                           # withdrawn, and kept out by the next run
        assert "REC-3286111" not in _notices()   # no row: neither matcher pass can attach it
        run(docs)
    run([_doc("NTS", _NTS, "638916")])           # the discontinuance no longer applies
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    assert _notices()["REC-3286111"].is_active is (date(2027, 3, 19) >= today)


def test_a_withdrawal_clears_lead_stamps_before_removing_the_row():
    """Leads stamped with the notice are cleared first (the same statement a cancellation
    runs), then the row goes; a cancelled row is never deleted."""
    import inspect

    src = inspect.getsource(nts_crawler._cancel_notices)
    assert src.index("UPDATE results SET auction_date = NULL") < src.index("DELETE FROM nts_notices")
    assert "AND cancelled_at IS NULL" in src[src.index("DELETE FROM nts_notices"):]


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


def test_a_published_notice_that_now_reads_as_held_is_withdrawn_not_cancelled(crawl):
    run, _ = crawl
    run([_doc("NTS", _NTS, "638916")])
    edited = _variant(instrument="3286111", remarks="SALE POSTPONED, SEE TRUSTEE")
    summary = run([_doc("NTS", edited, "638916")])
    assert "REC-3286111" not in _notices()          # comes back if it parses again
    assert summary["held"] == {"no_date": 1} and summary["withdrawn"] == 1


def test_a_cancelled_notice_stays_on_record_when_it_later_reads_as_held(crawl):
    run, _ = crawl
    ndts = _variant(instrument="3290000", doc_type="NDTS - NOTICE OF DISCONTINUANCE", remarks="",
                    related="3286111 [NTS]")
    run([_doc("NTS", _NTS, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    edited = _variant(instrument="3286111", remarks="SALE POSTPONED, SEE TRUSTEE")
    run([_doc("NTS", edited, "638916"), _doc("NDTS", ndts, "640000", instrument="3290000")])
    assert _notices()["REC-3286111"].cancelled_at is not None


def test_a_page_that_does_not_parse_writes_nothing(crawl):
    run, alerts = crawl
    summary = run([_doc("NTS", _NTS, "638916"), _doc("NDTS", "<html>Error 5001</html>", "640000")])
    assert _notices() == {}                      # the notice is NOT stored without its discontinuance
    assert summary.get("error") and alerts and "nothing written" in alerts[0][0]
