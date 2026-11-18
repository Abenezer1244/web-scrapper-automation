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
    confirm_image_parcel,
    reject_recorder_document,
    relocate_recorder_entries,
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
         recorded="11/12/2026", entry="3799745-0") -> dict:
    return {"item_id": item, "entry": entry, "instrument": instrument, "doc_code": "NTTRUS", "record_date": recorded,
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


def _nod(session, related, item="38772177", instrument="3799351", recorded="11/13/2026",
         entry="3799351-0") -> str:
    doc = {"item_id": item, "entry": entry, "instrument": instrument, "doc_code": "NOD", "record_date": recorded}
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


# ── the recorder re-indexed (2026-11-17: every entry id moved ~+9963) ───────────

def _rows(session, instrument: str) -> list:
    return session.execute(select(Doc).where(Doc.county == "cowlitz",
                                             Doc.instrument_number == instrument)).scalars().all()


def test_a_reindexed_notice_moves_to_its_new_id_and_is_not_staged_twice(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    moved = _nts(item="38782534")
    assert relocate_recorder_entries(db, "cowlitz", "WA", [moved]) == 1
    db.flush()
    row = db.get(Doc, doc_id)
    assert (row.transaction_item_id, row.entry_name, row.status) == ("38782534", "3799745-0", "pending_review")
    # Staging the rediscovered recording finds the same row: kept, never a second one.
    assert stage_recorder_document(
        db, county="cowlitz", state="WA", doc=moved, detail_html=None, pdf=laserfiche_pdf(1),
        tesseract=_tool(tmp_path, "tess_ok", _OK), recording_apns=moved["recording_apns"],
        related_dts=moved["related"]) == "kept"
    assert [r.id for r in _rows(db, "3799745")] == [doc_id]
    assert relocate_recorder_entries(db, "cowlitz", "WA", [moved]) == 0     # nothing left to move


def test_an_approved_notice_published_link_moves_with_it(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    assert relocate_recorder_entries(db, "cowlitz", "WA", [_nts(item="38782534")]) == 1
    db.flush()
    n = _notice(db, "3799745")
    db.refresh(n)
    assert n.source_url == ("https://www.cowlitzinfo.net/WLAudPublic/DocView.aspx"
                            "?id=38782534&dbid=0&repo=CCIMAGES")


def test_a_sibling_entry_moving_leaves_the_approved_notices_link_alone(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    _stage(db, tmp_path, item="38772599", entry="3799745-1")   # a second entry, same recording
    db.flush()
    assert relocate_recorder_entries(db, "cowlitz", "WA", [_nts(item="38782599", entry="3799745-1")]) == 1
    db.flush()
    n = _notice(db, "3799745")
    db.refresh(n)
    assert n.source_url.endswith("?id=38772571&dbid=0&repo=CCIMAGES")


def test_a_row_staged_before_entry_names_is_claimed_by_its_id_then_followed(db, tmp_path):
    doc_id = _stage(db, tmp_path)
    db.get(Doc, doc_id).entry_name = None                          # as staged before migration 170
    db.flush()
    assert relocate_recorder_entries(db, "cowlitz", "WA", [_nts()]) == 1   # same id: named
    assert db.get(Doc, doc_id).entry_name == "3799745-0"
    assert relocate_recorder_entries(db, "cowlitz", "WA", [_nts(item="38782534")]) == 1
    assert db.get(Doc, doc_id).transaction_item_id == "38782534"


@pytest.mark.parametrize("other", [
    {"instrument": "3799746"}, {"recorded": "11/13/2026"}], ids=["instrument", "date"])
def test_an_entry_that_is_not_the_staged_recording_fails_the_run(db, tmp_path, other):
    _stage(db, tmp_path)
    with pytest.raises(ValueError, match="is not the staged recording"):
        relocate_recorder_entries(db, "cowlitz", "WA", [_nts(item="38782534", **other)])


def test_an_id_held_by_another_entry_fails_the_run(db, tmp_path):
    _stage(db, tmp_path)
    _stage(db, tmp_path, item="38782534", instrument="3799746", entry="3799746-0")
    with pytest.raises(ValueError, match="held by"):
        relocate_recorder_entries(db, "cowlitz", "WA", [_nts(item="38782534")])


def test_a_retry_from_a_stored_failure_keeps_the_entry_name(db, tmp_path):
    doc = _nts()
    assert stage_recorder_document(db, county="cowlitz", state="WA", doc=doc, detail_html=None, pdf=None,
                                   recording_apns=doc["recording_apns"], related_dts=doc["related"]) == "failed"
    retry = {k: v for k, v in doc.items() if k != "entry"}      # a stored failure carries no entry
    stage_recorder_document(db, county="cowlitz", state="WA", doc=retry, detail_html=None,
                            pdf=laserfiche_pdf(1), tesseract=_tool(tmp_path, "tess_ok", _OK),
                            recording_apns=doc["recording_apns"], related_dts=doc["related"])
    db.flush()
    (row,) = _rows(db, "3799745")
    assert (row.status, row.entry_name) == ("pending_review", "3799745-0")


def test_a_reindexed_discontinuance_is_moved_not_applied_again(db, tmp_path):
    approve_recorder_document(db, _stage(db, tmp_path), reviewer="op-1", values=_values())
    db.flush()
    assert _nod(db, ["3799745"]) == "discontinuance"
    nod = db.execute(select(Doc).where(Doc.doc_code == "NOD")).scalar_one()
    cancelled = list(nod.fields["cancelled"])
    moved = {"item_id": "38782140", "entry": "3799351-0", "instrument": "3799351", "doc_code": "NOD",
             "record_date": "11/13/2026"}
    assert relocate_recorder_entries(db, "cowlitz", "WA", [moved]) == 1
    assert _nod(db, ["3799745"], item="38782140") == "kept"
    db.refresh(nod)
    assert nod.transaction_item_id == "38782140" and nod.fields["cancelled"] == cancelled
    assert len(_rows(db, "3799351")) == 1


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
        {"item_id": "38772177", "entry": "3799351-0", "instrument": "3799351", "doc_code": "NOD", "record_date": "11/03/2026",
         "recording_apns": ["WG3623040"], "related": ["3717613", "3793143"], "href": _href(38772177)},
        {"item_id": "38772100", "entry": "3799307-0", "instrument": "3799307", "doc_code": "NTTRUS", "record_date": "11/03/2026",
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


def test_the_pdf_url_is_taken_from_the_request_not_a_download_event():
    """Production runs Chromium headed (Xvfb): a PDF opens in the viewer and no download
    event fires (first crawl 2026-11-16: 11 of 11 timed out). fetch_pdf must intercept
    the /PDF10/ request itself, abort the browser's own load, and refetch it capped."""
    import inspect

    src = inspect.getsource(LaserficheWebLinkScraper.fetch_pdf)
    assert "expect_download" not in src
    assert 'context.route("**/PDF10/**"' in src and "await route.abort()" in src
    assert 'context.unroute("**/PDF10/**"' in src
    assert "read_recorder_pdf" in src     # the capped, same-origin refetch is unchanged


def test_the_review_cli_reviews_a_cowlitz_notice_in_cowlitz(tmp_path, capsys, monkeypatch):
    """scripts/nts_review.py was written for Chelan: it looked every parcel up in Chelan
    and linked Chelan's image. A Cowlitz notice is looked up in Cowlitz, through the
    batch path Cowlitz pre-foreclosure enrichment uses, and links the Laserfiche viewer."""
    import importlib.util
    import uuid
    from pathlib import Path

    from sqlalchemy import text

    import src.scrapers.enrichment.county_gis as cg
    from src.utils.crypto import blind_index

    spec = importlib.util.spec_from_file_location(
        "nts_review", Path(__file__).resolve().parents[1] / "scripts" / "nts_review.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    looked_up: list[tuple[list[str], str]] = []

    def _county(parcel_ids, cfg, unreached=None):
        looked_up.append((list(parcel_ids), cfg["endpoint"]))
        return {p: {"property_address": "123 MAIN ST, KELSO, WA 98626", "mailing_address": None,
                    "matched": True} for p in parcel_ids}

    monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(cg, "_batch_query_county", _county)
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda *a, **k: {})

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
                doc_id = _stage(s, tmp_path)
                s.commit()
            email = f"op-{uuid.uuid4().hex[:8]}@example.test"
            conn.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, is_admin, mfa_enabled) "
                              "VALUES (gen_random_uuid(), :e, 'x', :h, true, true)"),
                         {"e": email, "h": blind_index(email)})

            def run(*argv):
                cli.main(["--operator-email", email, *argv], auth_factory=_session, session_factory=_session)

            run("list")
            assert f"{doc_id}  cowlitz " in capsys.readouterr().out
            run("show", "--doc", doc_id)
            out = capsys.readouterr().out
            assert "cowlitz, WA  3799745" in out
            assert "cowlitzinfo.net/WLAudPublic/DocView.aspx?id=38772571" in out
            run("approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(), "--parcel", "61896",
                "--grantor", "DOE, JANE", "--not-discontinued")
            assert "property address: 123 MAIN ST, KELSO, WA 98626" in capsys.readouterr().out
            assert looked_up == [(["61896"], cg._KNOWN_GIS_ENDPOINTS["cowlitz_WA"]["endpoint"])]
            with _session() as s:
                n = s.execute(select(NtsNotice).where(NtsNotice.source == "cowlitz_recorder",
                                                      NtsNotice.ts_number == "REC-3799745")).scalar_one()
                assert (n.county, n.parcel, n.property_address) == ("cowlitz", "61896",
                                                                           "123 MAIN ST, KELSO, WA 98626")
        finally:
            outer.rollback()


# ── a recording whose index lists no parcel (3798834, 3799365, 3799644) ─────────

_SITUS_ROW = {"property_address": "1430 HAZEL DELL RD", "property_city": "CASTLE ROCK",
              "property_zip": "98611", "mailing_address": None, "matched": True}
_PRINTED = "1430 Hazel Dell Rd, Castle Rock, WA 98611"


@pytest.fixture
def county_layer(monkeypatch):
    """The Cowlitz county layer answering for WJ2811002 only; the statewide one is never asked."""
    import src.scrapers.enrichment.county_gis as cg

    asked: list[list[str]] = []

    def _county(parcel_ids, cfg, unreached=None):
        asked.append(list(parcel_ids))
        return {p: dict(_SITUS_ROW) for p in parcel_ids if p == "WJ2811002"}

    def _never(*a, **k):
        raise AssertionError("statewide layer asked")

    monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(cg, "_batch_query_county", _county)
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", _never)
    return asked


def _unlisted(db, tmp_path) -> str:
    return _stage(db, tmp_path, item="38781620", instrument="3798834", apns=(), related=("3695865",),
                  recorded="10/22/2026", entry="3798834-0")


def test_a_parcel_printed_on_the_image_is_approved_when_the_county_confirms_its_address(
        db, tmp_path, county_layer):
    doc_id = _unlisted(db, tmp_path)
    evidence = confirm_image_parcel("cowlitz", "WA", "WJ2811002", _PRINTED)
    assert county_layer == [["WJ2811002"]]
    approve_recorder_document(db, doc_id, reviewer="op-1",
                              values=_values(parcel="WJ2811002", property_address=_PRINTED),
                              situs=evidence["situs"], image_parcel=evidence)
    db.flush()
    n = _notice(db, "3798834")
    assert (n.parcel, n.property_address, n.address_source) == (
        "WJ2811002", "1430 HAZEL DELL RD", "assessor_situs")
    row = db.get(Doc, doc_id)
    assert row.fields["approved"]["parcel_source"] == "notice_image"
    assert row.fields["recording_apns"] == []                   # the recording's facts unchanged


@pytest.mark.parametrize("parcel,address,match", [
    ("WJ2811002", "1431 Hazel Dell Rd, Castle Rock, WA 98611", "is not the address printed"),
    ("WJ2811002", "1430 Pacific Ave, Castle Rock, WA 98611", "is not the address printed"),
    ("WJ2811002", "1430 Hazel Dell Rd, Kelso, WA 98611", "the city differs"),
    ("WJ2811002", "1430 Hazel Dell Rd, Castle Rock, WA 98626", "the ZIP code differs"),
    ("WJ2811002", "1430 Hazel Dell Rd Unit 2, Castle Rock, WA 98611", "the unit cannot be matched"),
    ("WJ2811002", "1430 Hazel Dell Rd Lot 2, Castle Rock, WA 98611", "the unit cannot be matched"),
    ("WJ2811002", None, "--address"),
    ("3015103", "1430 Hazel Dell Rd, Castle Rock, WA 98611", "has no site address"),
    ("WJ 2811002", "1430 Hazel Dell Rd", "letters, digits and hyphens"),
    ("12-3", "1430 Hazel Dell Rd", "shorter than any parcel"),
], ids=["house-number", "street", "city", "zip", "unit", "lot", "no-address", "unknown-parcel", "space",
        "too-short"])
def test_an_image_parcel_the_county_does_not_confirm_is_refused(county_layer, parcel, address, match):
    with pytest.raises(ReviewError, match=match):
        confirm_image_parcel("cowlitz", "WA", parcel, address)


def test_hyphens_are_dropped_as_the_recorder_drops_them(monkeypatch):
    import src.scrapers.enrichment.county_gis as cg

    monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(cg, "_batch_query_county", lambda ids, cfg, unreached=None: {
        p: {"property_address": "1007 S 5TH AVE", "property_city": "KELSO", "property_zip": "98626",
            "matched": True} for p in ids if p == "23292"})
    assert confirm_image_parcel("cowlitz", "WA", "2-3292", "1007 S 5th Ave, Kelso, WA 98626")["parcel"] == "23292"


def test_image_evidence_is_refused_for_a_recording_that_lists_its_own_apns(db, tmp_path):
    doc_id = _stage(db, tmp_path)                                # lists 61896, 6190401
    with pytest.raises(ReviewError, match="lists its own APNs"):
        approve_recorder_document(db, doc_id, reviewer="op-1",
                                  values=_values(parcel="WJ2811002", property_address=_PRINTED),
                                  image_parcel=dict(_EVIDENCE))
    assert _notice(db, "3799745") is None


_EVIDENCE = {"county": "cowlitz", "state": "WA", "parcel": "WJ2811002", "situs": "1430 HAZEL DELL RD",
             "city": "CASTLE ROCK", "zip": "98611", "printed": _PRINTED}
_NOT_FOR = "not for this county, parcel and printed address"


@pytest.mark.parametrize("change,printed,match", [
    ({"county": "chelan"}, _PRINTED, _NOT_FOR),
    ({"parcel": "3015103"}, _PRINTED, _NOT_FOR),
    ({"situs": None}, _PRINTED, _NOT_FOR),
    ({}, "1431 Hazel Dell Rd, Castle Rock, WA 98611", _NOT_FOR),
    ({}, None, _NOT_FOR),
    # Evidence made up by a caller is re-judged at the boundary, not trusted.
    ({"situs": "9 OTHER RD"}, _PRINTED, "is not the parcel's site address"),
], ids=["other-county", "other-parcel", "no-situs", "other-printed", "no-printed", "fabricated"])
def test_image_evidence_for_anything_else_is_refused(db, tmp_path, change, printed, match):
    doc_id = _unlisted(db, tmp_path)
    with pytest.raises(ReviewError, match=match):
        approve_recorder_document(db, doc_id, reviewer="op-1",
                                  values=_values(parcel="WJ2811002", property_address=printed),
                                  image_parcel={**_EVIDENCE, **change})


def test_without_the_image_flag_an_unlisted_recording_says_how_to_proceed(db, tmp_path):
    doc_id = _unlisted(db, tmp_path)
    with pytest.raises(ReviewError, match="--parcel-from-image"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(parcel="WJ2811002"))


@pytest.mark.parametrize("printed,situs,agrees", [
    ("1430 Hazel Dell Rd Lot 2, Castle Rock, WA 98611", "1430 HAZEL DELL RD LOT 3", False),
    ("1430 Hazel Dell Rd Lot 2, Castle Rock, WA 98611", "1430 HAZEL DELL RD LOT 2", True),
    ("12 Main St #4B, Kelso, WA 98626", "12 MAIN ST APT 4B", False),
    ("12 Main St Bldg 2, Kelso, WA 98626", "12 MAIN ST", False),
    ("12 Main St, Kelso, WA 98626", "12 MAIN ST TRLR 7", False),
    ("1007 S 5th Ave, Kelso, WA 98626", "1007 S 5TH AVE", True),
    ("12 Main St Lot 2 #4, Kelso, WA 98626", "12 MAIN ST LOT 3 #4", False),
    ("12 Main St Lot 2 #4, Kelso, WA 98626", "12 MAIN ST LOT 2 #4", True),
    ("12 Main St Lot 2 #4, Kelso, WA 98626", "12 MAIN ST LOT 2 4", False),
    ("12 Main St # 4, Kelso, WA 98626", "12 MAIN ST #4", True),
], ids=["other-lot", "same-lot", "marker-differs", "printed-bldg", "layer-trailer", "plain", "lot-and-hash", "same-lot-hash", "hash-vs-bare",
        "hash-spacing"])
def test_units_are_compared_as_the_street_key_strips_them(printed, situs, agrees):
    from src.workers.nts_crawler import image_address_disagreement

    assert (image_address_disagreement(printed, situs, None, None) is None) is agrees
