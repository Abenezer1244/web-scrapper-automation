"""Cowlitz recorded NTS: discovery facts, staging, review and discontinuance (Phase 3b).

The review pipeline is Chelan's, now per county. Recording facts are the live ones
(2026-11-15): NTS 3799745 (code NTTRUS, parcels 61896 + 6190401, related AFN 3718972
= its deed of trust) and discontinuance 3799351 (code DISTRS, related AFNs 3717613 and
3793143). Staged reads come from the real staging path with a stand-in tesseract and a
PDF in Laserfiche's CCITT shape. Real test database, rolled back per test.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import NtsNotice
from src.db.models import NtsRecorderDocument as Doc
from src.db.session import sync_engine
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.templates.laserfiche_weblink import LaserficheWebLinkScraper
from src.workers.nts_crawler import (
    COWLITZ_RECORDER_SOURCE,
    ReviewError,
    approve_recorder_document,
    reject_recorder_document,
    stage_discontinuance,
    stage_recorder_document,
)
from tests.test_nts_recorder_ocr import _OK, _tool, laserfiche_pdf
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_SALE = date.today() + timedelta(days=60)
_BASE = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"


@pytest.fixture
def db():
    """One outer transaction, rolled back after the test (as the Chelan suites)."""
    with sync_engine.connect() as conn:
        outer = conn.begin()

        @contextmanager
        def _session():
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        try:
            with _session() as s:
                yield s
        finally:
            outer.rollback()


def _nts(item="38772571", instrument="3799745", apns=("61896",), related=("3718972",),
         recorded="11/12/2026") -> dict:
    return {"item_id": item, "instrument": instrument, "doc_code": "NTTRUS", "record_date": recorded,
            "recording_apns": list(apns), "related": list(related)}


def _stage(session, tmp_path, **kw) -> str:
    doc = _nts(**kw)
    status = stage_recorder_document(
        session, county="cowlitz", state="WA", doc=doc, detail_html=None, pdf=laserfiche_pdf(1),
        tesseract=_tool(tmp_path, "tess_ok", _OK),
        recording_apns=doc["recording_apns"], related_dts=doc["related"])
    assert status == "pending_review"
    session.flush()
    return str(session.execute(select(Doc.id).where(
        Doc.county == "cowlitz", Doc.transaction_item_id == doc["item_id"])).scalar_one())


def _nod(session, related, item="38772177", instrument="3799351", recorded="11/13/2026") -> str:
    doc = {"item_id": item, "instrument": instrument, "doc_code": "NOD", "record_date": recorded}
    status = stage_discontinuance(session, county="cowlitz", state="WA", doc=doc, detail_html=None,
                                  related_afns=list(related), recording_apns=["WG3623040"])
    session.flush()
    return status


def _notice(session, instrument: str):
    return session.execute(select(NtsNotice).where(
        NtsNotice.source == COWLITZ_RECORDER_SOURCE,
        NtsNotice.ts_number == f"REC-{instrument}")).scalar_one_or_none()


def _values(**kw) -> dict:
    return {"auction_date": _SALE, "parcel": "61896", "grantor": "SAMPLE BORROWER", **kw}


# ── staging and review ─────────────────────────────────────────────────────────

def test_a_cowlitz_notice_is_staged_with_its_recordings_own_facts(db, tmp_path):
    doc = db.get(Doc, _stage(db, tmp_path))
    assert (doc.county, doc.status, doc.doc_code, doc.instrument_number) == (
        "cowlitz", "pending_review", "NTTRUS", "3799745")
    assert doc.fields["recording_apns"] == ["61896"]
    assert doc.fields["related_deeds_of_trust"] == ["3718972"]
    assert doc.recording_apn == "61896" and doc.related_deed_of_trust == "3718972"


def test_approval_writes_a_cowlitz_notice_linked_to_the_recorders_viewer(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())
    db.flush()
    n = _notice(db, "3799745")
    assert (n.county, n.state, n.parcel, n.auction_date) == ("cowlitz", "WA", "61896", _SALE)
    assert n.is_active and n.cancelled_at is None
    assert n.source_url == ("https://www.cowlitzinfo.net/WLAudPublic/DocView.aspx"
                            "?id=38772571&dbid=0&repo=CCIMAGES")


def test_the_parcel_must_be_one_the_recording_names(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    with pytest.raises(ReviewError, match="not one of this recording's own APNs"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(parcel="6190401"))
    assert _notice(db, "3799745") is None


def test_a_cowlitz_notice_can_be_rejected(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    reject_recorder_document(db, doc_id, reviewer="op-1", reason="unreadable")
    db.flush()
    assert db.get(Doc, doc_id).status == "rejected" and _notice(db, "3799745") is None


def test_a_county_without_a_review_pipeline_is_refused(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    db.get(Doc, doc_id).county = "kitsap"
    db.flush()
    with pytest.raises(ReviewError, match="not reviewed here"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())


# ── discontinuances (no OCR: their related numbers are entry properties) ───────

def test_a_discontinuance_naming_an_approved_notice_cancels_it(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    assert _nod(db, ["3718972", "3799745"]) == "discontinuance"
    n = _notice(db, "3799745")
    assert n.cancelled_at is not None and not n.is_active


def test_a_discontinuance_citing_only_the_loan_waits_for_an_operator(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    assert _nod(db, ["3718972"]) == "pending_review"
    assert _notice(db, "3799745").cancelled_at is None


def test_a_discontinuance_found_first_cancels_the_notice_when_approved(db, tmp_path):
    assert _nod(db, ["3799745"]) == "discontinuance"   # kept as evidence
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    assert _notice(db, "3799745").cancelled_at is not None


def test_a_discontinuance_naming_another_recording_cancels_nothing(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    _nod(db, ["9999999"])
    assert _notice(db, "3799745").cancelled_at is None


# ── discovery: facts from the entry properties, codes checked ──────────────────

class _Portal(LaserficheWebLinkScraper):
    def __init__(self, rows, fields):
        super().__init__(_BASE, "cowlitz", "WA", record_type="pre_foreclosure")
        self.rows, self.fields = rows, fields

    async def _collect(self, date_from, date_to):
        return [dict(r) for r in self.rows]

    async def _read_entry(self, row):
        return self.fields[row["afn"]]


def _href(docid: int) -> str:
    return f"/WLAUDPublic/DocView.aspx?id={docid}&dbid=0&repo=CCIMAGES&searchid=x"


def _row(afn, doc_type, docid):
    return {"afn": afn, "date": "11/3/2026", "doc_type": doc_type, "href": _href(docid)}


@pytest.mark.asyncio
async def test_discover_returns_notices_and_discontinuances_with_their_facts():
    portal = _Portal(
        [_row("3799351-0", "Discont Of Trustees Sale", 38772177),
         _row("3799307-0", "Notice Of Trustees Sale", 38772100),
         _row("3799306-0", "Deed Of Trust", 38772099)],
        {"3799351-0": {"Rec Doc Document Type": ["DISTRS"], "Rec Doc Related AFN": ["3717613", "3793143"],
                       "Rec Doc Parcel": ["WG3623040"]},
         "3799307-0": {"Rec Doc Document Type": ["NTTRUS"], "Rec Doc Related AFN": ["3665592"],
                       "Rec Doc Parcel": ["0278801"]}})
    docs = await portal.discover("11/03/2026", "11/03/2026")
    assert docs == [
        {"item_id": "38772177", "instrument": "3799351", "doc_code": "NOD", "record_date": "11/03/2026",
         "recording_apns": ["WG3623040"], "related": ["3717613", "3793143"], "href": _href(38772177)},
        {"item_id": "38772100", "instrument": "3799307", "doc_code": "NTTRUS", "record_date": "11/03/2026",
         "recording_apns": ["0278801"], "related": ["3665592"], "href": _href(38772100)},
    ]


@pytest.mark.asyncio
async def test_a_label_and_code_that_disagree_fail_discovery():
    portal = _Portal([_row("3799351-0", "Discont Of Trustees Sale", 38772177)],
                     {"3799351-0": {"Rec Doc Document Type": ["NTTRUS"]}})
    with pytest.raises(ScraperExecutionError, match="carries code"):
        await portal.discover("11/03/2026", "11/03/2026")


# ── the PDF read: own recorder only, capped, a real PDF (Codex 3b P2) ──────────

_ROOT = "https://www.cowlitzinfo.net/WLAudPublic"
_URL = "https://www.cowlitzinfo.net/WLAUDPublic/PDF10/0b56f4e9/38772571"


def _writer(body: bytes, calls: list | None = None):
    def fetch(url, path, cap):
        if calls is not None:
            calls.append(url)
        with open(path, "wb") as fh:
            fh.write(body)
    return fetch


def test_the_recorders_pdf_is_read():
    from src.scrapers.templates.laserfiche_weblink import read_recorder_pdf

    assert read_recorder_pdf(_URL, _ROOT, 1000, _writer(b"%PDF-1.4 x")) == b"%PDF-1.4 x"


@pytest.mark.parametrize("url", [
    "https://evil.example/PDF10/x/1",
    "http://www.cowlitzinfo.net/WLAUDPublic/PDF10/x/1",
    "https://www.cowlitzinfo.net:8443/WLAUDPublic/PDF10/x/1",   # same host, another port
])
def test_a_pdf_from_another_host_or_scheme_is_never_fetched(url):
    from src.scrapers.templates.laserfiche_weblink import read_recorder_pdf

    calls: list = []
    with pytest.raises(ValueError, match="another host"):
        read_recorder_pdf(url, _ROOT, 1000, _writer(b"%PDF-", calls))
    assert calls == []


@pytest.mark.parametrize("fetch,match", [
    (_writer(b"<html>session expired</html>"), "not a PDF"),
    (_writer(b"%PDF-" + b"x" * 2000), "over 1000 bytes"),   # a fetch that did not stop at the cap
])
def test_a_bad_download_fails(fetch, match):
    from src.scrapers.templates.laserfiche_weblink import read_recorder_pdf

    with pytest.raises(ValueError, match=match):
        read_recorder_pdf(_URL, _ROOT, 1000, fetch)


def test_a_failed_download_fails_with_its_reason():
    from src.scrapers.templates.laserfiche_weblink import read_recorder_pdf

    def boom(url, path, cap):
        raise TimeoutError("stalled")

    with pytest.raises(ValueError, match="download failed .TimeoutError."):
        read_recorder_pdf(_URL, _ROOT, 1000, boom)


# ── crawl accounting (Codex 3b P1) ─────────────────────────────────────────────

def test_stored_failures_past_the_retry_share_count_as_unread():
    from src.workers.nts_crawler import _CHELAN_RETRY_SHARE, chelan_work_order, deferred_retries

    failures = [{"item_id": str(i), "doc_code": "NTTRUS"} for i in range(_CHELAN_RETRY_SHARE + 1)]
    order = chelan_work_order(failures, failures)       # every one rediscovered, too
    assert len(order) == _CHELAN_RETRY_SHARE
    assert deferred_retries(failures) == 1               # the sixth: unread, never "fresh"


def test_the_cowlitz_crawl_reaches_back_after_an_outage_and_counts_deferred_work():
    import inspect

    from src.workers import nts_crawler

    src = inspect.getsource(nts_crawler.crawl_nts_cowlitz_recorder)
    assert "chelan_window_start(today, last_read)" in src          # not a fixed 30 days
    assert 'summary["unread"] = deferred_retries(retries)' in src
    assert src.index("chelan_cache_is_fresh(summary)") < src.index('_record_crawl_heartbeat(db, "cowlitz")')
    # each recording is read and staged inside the budget, not all OCR after it
    assert src.index("_COWLITZ_DOC_RESERVE_S") < src.index("asyncio.to_thread(_stage_one")



def test_an_outage_longer_than_the_reach_is_reported_and_never_fresh():
    from datetime import UTC, datetime

    from src.workers.nts_crawler import _CHELAN_MAX_REACH_DAYS, recorder_gap_beyond_reach

    today = date(2026, 11, 15)
    assert recorder_gap_beyond_reach(today, None) is None                       # first run
    recent = datetime(2026, 11, 1, tzinfo=UTC)
    assert recorder_gap_beyond_reach(today, recent) is None
    long_ago = datetime.combine(today - timedelta(days=_CHELAN_MAX_REACH_DAYS + 10),
                                datetime.min.time(), tzinfo=UTC)
    assert "never read" in recorder_gap_beyond_reach(today, long_ago)


def test_the_cowlitz_crawl_withholds_freshness_over_a_gap_and_rechecks_before_staging():
    import inspect

    from src.workers import nts_crawler

    src = inspect.getsource(nts_crawler.crawl_nts_cowlitz_recorder)
    assert "chelan_cache_is_fresh(summary) and gap is None" in src
    assert src.index("_COWLITZ_STAGE_RESERVE_S") < src.index("asyncio.to_thread(_stage_one")
