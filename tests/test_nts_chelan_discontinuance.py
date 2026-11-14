"""Chelan recorded Notices of Discontinuance (4a-3c): read from the detail page, applied
to the approved notice they name, held for an operator when they only cite a loan.

Real test database, one outer transaction rolled back at the end. The discontinuance
page is Chelan's real one (AFN 2628164, names redacted), whose Related Number block
links an untagged 2248127 and "2625826 [NTS]"; variants swap only those numbers.
"""
from __future__ import annotations

# Fixtures imported from the approval tests and taken as parameter names read as
# redefinitions to ruff. Only F811, only this file.
# ruff: noqa: F811
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import NtsNotice
from src.db.models import NtsRecorderDocument as Doc
from src.scrapers.sources.nts_recorder_ocr import detail_doc_code, related_numbers
from src.workers.nts_crawler import (
    CHELAN_RECORDER_SOURCE,
    ReviewError,
    approve_recorder_document,
    chelan_window_start,
    reject_recorder_document,
    resolve_discontinuance,
    stage_discontinuance,
)
from tests.test_nts_chelan_approval import _APN, _DT, _stage, _values, c, db  # noqa: F401
from tests.test_nts_chelan_recorder_stage import _DETAIL
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_NOD = (Path(__file__).parent / "fixtures" / "acclaimweb_chelan" / "detail_nod_1790020.html").read_text(
    encoding="utf-8")
_NTS_NO = "2625826"   # the notice the real discontinuance names, tagged [NTS]


def _nod_page(direct: str | None = _NTS_NO, other: str = "2248127", tag: str = "NTS") -> str:
    page = _NOD.replace("2248127", other)
    if direct is None:
        return page.replace(f"{_NTS_NO} [NTS]", f"{_DT} [DT]")   # cites only the loan
    return page.replace(f"{_NTS_NO} [NTS]", f"{direct} [{tag}]")


def _stage_nod(db, html: str | None, item: str = "9700001", instrument: str = "2628164",
               recorded: str = "10/13/2026") -> str:
    status = stage_discontinuance(db, county="chelan", state="WA", detail_html=html,
                                  doc={"item_id": item, "instrument": instrument, "doc_code": "NOD",
                                       "record_date": recorded})
    db.flush()
    return status


def _nod_row(db, item: str = "9700001") -> Doc:
    return db.execute(select(Doc).where(Doc.transaction_item_id == item)).scalar_one()


def _notice(db, instrument: str) -> NtsNotice:
    db.expire_all()
    return db.execute(select(NtsNotice).where(NtsNotice.source == CHELAN_RECORDER_SOURCE,
                                              NtsNotice.ts_number == f"REC-{instrument}")).scalar_one()


def _approve(db, tmp_path, item: str, instrument: str, recorded: str = "09/01/2026", **kw) -> str:
    doc_id = _stage(db, tmp_path, item, instrument, recorded)
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(), **kw)
    db.flush()
    return doc_id


# ── reading the page ──

def test_the_real_discontinuance_page_names_its_type_and_every_related_document():
    assert detail_doc_code(_NOD) == "NOD"
    assert related_numbers(_NOD) == [("2248127", None), (_NTS_NO, "NTS")]
    assert detail_doc_code(_DETAIL) == "NTS" and related_numbers(_DETAIL) == [(_DT, "DT")]
    assert related_numbers("<html>maintenance</html>") is None


# ── applied by the crawl ──

def test_a_discontinuance_naming_an_approved_notice_cancels_it(db, tmp_path):
    _approve(db, tmp_path, "9900101", _NTS_NO)
    assert _stage_nod(db, _nod_page()) == "discontinuance"
    n = _notice(db, _NTS_NO)
    assert n.cancelled_at is not None and n.is_active is False
    assert _nod_row(db).fields["cancelled"] == [{"instrument": _NTS_NO, "basis": "direct"}]
    # Read again by a later crawl: settled, nothing applied twice.
    assert _stage_nod(db, _nod_page()) == "kept"


def test_a_discontinuance_found_first_is_applied_when_its_notice_is_approved(db, tmp_path):
    assert _stage_nod(db, _nod_page()) == "discontinuance"   # evidence, no approved notice yet
    doc_id = _approve(db, tmp_path, "9900102", _NTS_NO)
    assert _notice(db, _NTS_NO).cancelled_at is not None
    assert "discontinued by 2628164" in db.get(Doc, doc_id).decision_note
    assert _nod_row(db).fields["cancelled"] == [{"instrument": _NTS_NO, "basis": "direct"}]


def test_a_discontinuance_recorded_before_a_notice_never_ends_it(db, tmp_path):
    _stage_nod(db, _nod_page(), recorded="08/01/2026")
    _approve(db, tmp_path, "9900103", _NTS_NO, recorded="09/01/2026")
    n = _notice(db, _NTS_NO)
    assert n.cancelled_at is None and n.is_active


def test_a_discontinuance_citing_only_the_loan_waits_for_an_operator(db, tmp_path):
    _approve(db, tmp_path, "9900104", "2627524")              # its deed of trust is _DT
    assert _stage_nod(db, _nod_page(direct=None)) == "pending_review"
    assert _notice(db, "2627524").cancelled_at is None        # nothing guessed
    nod = _nod_row(db)
    assert "2627524" in nod.decision_note and nod.ocr_text is None
    with pytest.raises(ReviewError, match="must name approved, live notices"):
        resolve_discontinuance(db, str(nod.id), reviewer="op-2", cancels=["2999999"])
    resolve_discontinuance(db, str(nod.id), reviewer="op-2", cancels=["2627524"])
    db.flush()
    assert _notice(db, "2627524").cancelled_at is not None
    nod = _nod_row(db)
    assert (nod.status, nod.reviewed_by) == ("discontinuance", "op-2")
    assert nod.fields["cancelled"] == [{"instrument": "2627524", "basis": "operator"}]


def test_an_operator_can_find_a_loan_discontinuance_ends_no_live_sale(db, tmp_path):
    _approve(db, tmp_path, "9900105", "2627524")
    _stage_nod(db, _nod_page(direct=None))
    resolve_discontinuance(db, str(_nod_row(db).id), reviewer="op-2", cancels=[])
    db.flush()
    assert _notice(db, "2627524").cancelled_at is None
    assert _nod_row(db).decision_note == "operator: ends no approved sale"


def test_a_page_that_links_nothing_waits_for_an_operator(db):
    page = _NOD.replace("2248127", "x").replace(f"{_NTS_NO} [NTS]", "y")   # no number links
    assert related_numbers(page) == []
    assert _stage_nod(db, page) == "pending_review"
    assert "links no related document" in _nod_row(db).decision_note


@pytest.mark.parametrize("html,note", [
    (None, "detail page not downloaded"),
    (_DETAIL, "not a discontinuance detail page (NTS)"),
    ("<html>maintenance</html>", "not a discontinuance detail page (None)"),
], ids=["not-downloaded", "wrong-document", "not-a-detail-page"])
def test_an_unreadable_discontinuance_fails_and_is_retried(db, html, note):
    assert _stage_nod(db, html) == "failed"
    assert _nod_row(db).decision_note == note
    assert _stage_nod(db, _nod_page()) == "discontinuance"   # the retry reads it


# ── approval facing a loan discontinuance on file ──

def test_approval_must_answer_a_loan_discontinuance_on_file(db, tmp_path):
    _stage_nod(db, _nod_page(direct=None))                     # cites _DT, no notice approved yet
    assert _nod_row(db).status == "discontinuance"
    doc_id = _stage(db, tmp_path, "9900106", "2627524")
    with pytest.raises(ReviewError, match="2628164 cite this notice's loan"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())
    with pytest.raises(ReviewError, match="names no discontinuance"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(), discontinued_by="1")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(), discontinued_by="2628164")
    db.flush()
    assert _notice(db, "2627524").cancelled_at is not None
    assert _nod_row(db).fields["cancelled"] == [{"instrument": "2627524", "basis": "operator"}]


def test_an_operator_can_approve_past_a_loan_discontinuance_that_ended_an_earlier_sale(db, tmp_path):
    _stage_nod(db, _nod_page(direct=None))
    doc_id = _stage(db, tmp_path, "9900107", "2627525")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(), not_discontinued=True)
    db.flush()
    assert _notice(db, "2627525").is_active
    assert "not discontinued" in db.get(Doc, doc_id).decision_note


def test_a_discontinuance_is_resolved_not_approved_or_rejected(db):
    _stage_nod(db, _nod_page(direct=None, other="2248127"))
    nod_id = str(_nod_row(db).id)
    db.execute(Doc.__table__.update().where(Doc.id == nod_id).values(status="pending_review"))
    db.expire_all()
    with pytest.raises(ReviewError, match="use resolve-nod"):
        approve_recorder_document(db, nod_id, reviewer="op-1", values=_values())
    with pytest.raises(ReviewError, match="resolved with resolve-nod"):
        reject_recorder_document(db, nod_id, reviewer="op-1", reason="unreadable")


# ── where the crawl starts ──

@pytest.mark.parametrize("last_read_days_ago,start_days_ago", [
    (None, 30),     # first run: the window holds every stageable notice and its discontinuances
    (1, 30),        # daily: the usual overlapping window
    (45, 47),       # after an outage: back to the last complete read, with overlap
    (200, 60),      # never past the reach limit
], ids=["first-run", "daily", "outage", "long-outage"])
def test_the_search_reaches_back_over_a_gap(last_read_days_ago, start_days_ago):
    from datetime import UTC, datetime

    today = date(2026, 11, 14)
    last = None if last_read_days_ago is None else (
        datetime(2026, 11, 14, 10, tzinfo=UTC) - timedelta(days=last_read_days_ago))
    assert chelan_window_start(today, last) == today - timedelta(days=start_days_ago)


def test_the_crawl_finds_discontinuances_and_retries_stored_failures():
    import inspect

    import src.workers.nts_crawler as crawler

    src = inspect.getsource(crawler.crawl_nts_chelan_recorder)
    assert '_CHELAN_NTS_CODES | {"NOD"}' in src
    assert "stage_discontinuance(" in src and "stored_failures" in src
    assert "chelan_window_start(" in src


# ── Codex review 1 regressions ──

def test_a_pending_discontinuance_never_expires_and_still_blocks_a_later_approval(db, tmp_path):
    from sqlalchemy import text

    from src.workers.nts_crawler import purge_stale_recorder_documents

    _stage_nod(db, _nod_page(direct=None, other="2248127"))        # loan-only, nothing approved
    db.execute(text("UPDATE nts_recorder_documents SET status = 'pending_review', "
                    "created_at = now() - interval '90 days' WHERE transaction_item_id = '9700001'"))
    purge_stale_recorder_documents(db)
    db.flush()
    db.expire_all()
    nod = _nod_row(db)
    assert nod.status == "pending_review" and nod.fields["related"]   # evidence kept
    doc_id = _stage(db, tmp_path, "9900108", "2627526")
    with pytest.raises(ReviewError, match="cite this notice's loan"):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())


def test_a_direct_match_does_not_settle_a_discontinuance_that_also_cites_another_loan(db, tmp_path):
    _approve(db, tmp_path, "9900109", _NTS_NO)                    # named directly
    _approve(db, tmp_path, "9900110", "2627527", independent=True)  # shares _DT with the first
    # The discontinuance names 2625826 directly and also cites the deed of trust _DT.
    assert _stage_nod(db, _nod_page(other=_DT)) == "pending_review"
    assert _notice(db, _NTS_NO).cancelled_at is not None          # the direct one ends at once
    assert _notice(db, "2627527").cancelled_at is None            # the other waits
    nod = _nod_row(db)
    assert "2627527" in nod.decision_note
    resolve_discontinuance(db, str(nod.id), reviewer="op-2", cancels=["2627527"])
    db.flush()
    assert _notice(db, "2627527").cancelled_at is not None
    assert _nod_row(db).fields["cancelled"] == [
        {"instrument": _NTS_NO, "basis": "direct"}, {"instrument": "2627527", "basis": "operator"}]


def test_discontinuances_go_first_and_failures_get_a_bounded_rotating_share():
    from src.workers.nts_crawler import _CHELAN_MAX_DOCS, _CHELAN_RETRY_SHARE, chelan_work_order

    def d(i, code):
        return {"item_id": str(i), "instrument": str(i), "doc_code": code, "record_date": "10/01/2026"}

    discovered = [d(1, "NTS"), d(2, "NOD"), d(3, "NTSCOM")]
    retries = [d(100 + i, "NTS") for i in range(40)]              # failures that never read
    order = chelan_work_order(discovered, retries)
    assert order[0]["doc_code"] == "NOD"
    assert [x["item_id"] for x in order[1:3]] == ["1", "3"]        # this window's notices next
    assert len(order) == 3 + _CHELAN_RETRY_SHARE <= _CHELAN_MAX_DOCS
    # A discontinuance stuck behind failures is still read first.
    assert chelan_work_order([], [d(9, "NTS"), d(10, "NOD")])[0]["item_id"] == "10"


def test_failures_the_search_finds_again_still_take_only_the_retry_share():
    from src.workers.nts_crawler import _CHELAN_RETRY_SHARE, chelan_work_order

    def d(i, code="NTS"):
        return {"item_id": str(i), "instrument": str(i), "doc_code": code, "record_date": "10/01/2026"}

    failures = [d(100 + i) for i in range(40)]
    # Discovery runs oldest day first: the 40 failures come back before the new notice.
    order = chelan_work_order([*failures, d(1)], failures)
    ids = [x["item_id"] for x in order]
    assert ids[0] == "1"                                          # the new notice is read
    assert len(order) == 1 + _CHELAN_RETRY_SHARE and len(set(ids)) == len(ids)
