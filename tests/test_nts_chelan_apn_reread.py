"""Approval re-reads a recording's APNs when the crawl stored none.

Chelan indexes a recording's APN some time after recording it. Recording 2629393 was
crawled the day after it was recorded, its detail page then listed no APN, and the page
has since listed APN 252104732030, the parcel its image names. Approval reads the page
again, and the parcel must be one it lists now. Staged rows come from the real staging
path (Chelan's real detail page) with the stored APN list emptied, as the crawl stored
it for 2629393. The recorder page is an external site: the fetch is replaced with that
real page, as the staging tests do.
"""
# ruff: noqa: F811
from __future__ import annotations

import pytest
from sqlalchemy import text

import src.workers.nts_crawler as crawler
from src.db.models import NtsRecorderDocument as Doc
from src.workers.nts_crawler import (
    ReviewError,
    approve_recorder_document,
    chelan_live_recording_apns,
)
from tests.test_nts_chelan_approval import (  # noqa: F401 -- fixtures and helpers
    _APN,
    _SALE,
    _notice,
    _operator,
    _run,
    _stage,
    _values,
    c,
    db,
)
from tests.test_nts_chelan_recorder_stage import _DETAIL
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring


def _stage_with_no_apn(db, tmp_path, item: str, instrument: str) -> str:
    doc_id = _stage(db, tmp_path, item, instrument)
    doc = db.get(Doc, doc_id)
    doc.fields, doc.recording_apn = {**doc.fields, "recording_apns": []}, None
    db.flush()
    return doc_id


def test_the_live_page_is_read_and_an_unreadable_one_gives_nothing(monkeypatch):
    clean = _page("APN 222014846080", "LOT 7")

    def _fetch(url, *_a, **_k):
        if "/documentdetails/1/" in url:
            return clean.encode()
        raise OSError("unreachable")

    monkeypatch.setattr(crawler, "_bounded_fetch", _fetch)
    assert chelan_live_recording_apns("1") == [_APN]
    assert chelan_live_recording_apns("2") is None


def test_an_apn_indexed_after_the_crawl_is_used_and_stored(db, tmp_path):
    doc_id = _stage_with_no_apn(db, tmp_path, "9930001", "2630001")
    approve_recorder_document(db, doc_id, reviewer="op-1", live_apns=[_APN], values=_values())
    db.flush()
    assert _notice(db, "2630001").parcel == _APN
    d = db.get(Doc, doc_id)
    assert (d.fields["recording_apns"], d.recording_apn) == ([_APN], _APN)
    assert "re-read from the recorder page" in d.decision_note


@pytest.mark.parametrize("stored,live,county,match", [
    ([], None, "chelan", "own APNs"),                  # page unreadable: nothing to go on
    ([], [], "chelan", "own APNs"),                    # still no APN on the page
    ([], ["111111111111"], "chelan", "own APNs"),      # the page lists another parcel
    (None, [_APN], "chelan", "own APNs"),              # APNs unknown is not "none stored"
    (["111111111111"], [_APN], "chelan", "own APNs"),  # stored APNs are never replaced
    ([], [_APN], "cowlitz", "Chelan recordings only"),
], ids=["unreadable", "still-none", "other-parcel", "unknown", "stored-wins", "not-chelan"])
def test_a_re_read_never_widens_what_the_recording_says(db, tmp_path, stored, live, county, match):
    doc_id = _stage(db, tmp_path, "9930002", "2630002")
    doc = db.get(Doc, doc_id)
    doc.fields, doc.county = {**doc.fields, "recording_apns": stored}, county
    db.flush()
    with pytest.raises(ReviewError, match=match):
        approve_recorder_document(db, doc_id, reviewer="op-1", live_apns=live, values=_values())
    assert db.get(Doc, doc_id).status == "pending_review"


def test_the_cli_re_reads_the_page_when_no_apn_was_stored(c, db, tmp_path, capsys, monkeypatch):
    fetched: list[str] = []

    def _fetch(url, *_a, **_k):
        fetched.append(url)
        return _page("APN 222014846080", "LOT 7").encode()

    monkeypatch.setattr(crawler, "_bounded_fetch", _fetch)
    doc_id = _stage_with_no_apn(db, tmp_path, "9930003", "2630003")
    db.commit()
    email = _operator(c)
    _run(c, email, "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(), "--parcel", _APN,
         "--grantor", "DOE, JANE")
    out = capsys.readouterr().out
    assert "the recorder page lists now: ['222014846080']" in out and "approved: notice" in out
    assert len(fetched) == 1 and "/documentdetails/9930003/" in fetched[0]
    assert c.execute(text("SELECT parcel FROM nts_notices WHERE ts_number = 'REC-2630003'")).scalar() == _APN


def test_the_cli_does_not_re_read_when_the_crawl_stored_apns(c, db, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "_bounded_fetch", lambda *a, **k: pytest.fail("no fetch expected"))
    doc_id = _stage(db, tmp_path, "9930004", "2630004")
    db.commit()
    _run(c, _operator(c), "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(),
         "--parcel", _APN, "--grantor", "DOE, JANE")
    assert c.execute(text("SELECT parcel FROM nts_notices WHERE ts_number = 'REC-2630004'")).scalar() == _APN


_LEGAL_BLOCK = '<span class="" >APN 222014846080</span><br /><span class="" >SP 2019-008 LOT 7</span><br />'


def _page(*lines: str) -> str:
    """The real detail page with its DocLegal block replaced, one <span> per line."""
    assert _LEGAL_BLOCK in _DETAIL
    return _DETAIL.replace(_LEGAL_BLOCK, "".join(f'<span class="" >{x}</span><br />' for x in lines))


@pytest.mark.parametrize("lines,expected", [
    (["APN 222014846080", "SP 2019-008 LOT 7"], None),  # a plat number: unknown         # the real shape
    (["APN 252104732030", "NORTH ENTIAT LOT 2 BLK 2"], ["252104732030"]),  # 2629393
    (["APN 222014846080 252104732030"], ["222014846080", "252104732030"]),
    (["APN 222014846080", "APN 252104732030", "LOT 7"], ["222014846080", "252104732030"]),
    (["APN: 222014846080, 252104732030"], ["222014846080", "252104732030"]),
    (["APN #252104732030"], ["252104732030"]),
    (["APN 2220-1484-6080"], ["222014846080"]),
    (["APN 123456789", "SP 2019-008"], None),                          # 9 digits: not a Chelan APN
    (["APN 222014846080 2521 0473 2030"], None),                       # spaced second APN
    (["APN 2220 1484 6080"], None),                                    # spaced only
    (["APN 222014846080 &amp; 252104 732030"], None),                  # split after a separator
    (["APN 222014846080 &amp;252104 732030", "LOT 7"], None),          # glued separator
    (["APN 2521&nbsp;0473&nbsp;2030"], None),
    (["APN 222014846080", "APN NO. 252104 732030", "LOT 7"], None),    # label word, split APN
    (["APN 222014846080 PTN"], ["222014846080"]),                      # "portion of": a word after the APN
    # The shared parser stops at "TRUSTEE" and sees one APN; the block shows two. They
    # disagree, so the answer is unknown rather than the partial list (Codex).
    (["APN 222014846080", "LOT 7 AS DESCRIBED IN TRUSTEE'S DEED", "APN 252104732030"], None),
    (["APN 222014846080", "SEC 12 TWP 23 RGE 20", "12-23-20"], None),
    (["SP 2019-008 LOT 7"], None),                                    # plat numbers: unknown
    (["NORTH ENTIAT LOT 2 BLK 2"], []),                               # no APN, no number: none listed
    (["APN 222014846080", "AND (252104", "732030)"], None),           # Codex: parenthesised pieces                                       # no APN line: none listed
    (["APN 222014846080", "LOT 7 AS DESCRIBED IN TRUSTEE'S DEED", "APN NO. #"], None),
    (["APN"], None),                                                    # an empty APN line
    (["APN 222014846080", "LOT 7 AS DESCRIBED IN TRUSTEE&#39;S DEED", "ADDITIONAL APN 252104732030"],
     None),                                                             # an APN on any line counts
    (["APN 222014846080", "REC 2521 0473 2030"], None),                # spaced APN-size number
    (["APN 222014846080", "SP 2019-008 LOT 7 SEC 12 TWP 23 RGE 20 12-23-20"], None),
    (["APN 222014846080", "LOT 7 AS DESCRIBED IN TRUSTEE&#39;S DEED", "ADDITIONAL APN 2521-",
      "0473-2030"], None),                                              # APN split across a break
    (["APN 222014846080", "LOT 7 <span>2521</span><span>04732030</span>"], None),  # inline tags
    (["APN", "222014846080"], ["222014846080"]),                        # number on the next line
    (["APN 222014846080", "252104", "732030"], None),                   # APN in pieces on bare lines
    (["APN 222014846080", "252104732030", "LOT 7"], ["222014846080", "252104732030"]),
    (["APN 222014846080", "AND 252104", "732030"], None),               # "AND" then pieces
    (["APN 222014846080", "AND 252104732030"], ["222014846080", "252104732030"]),
    (["APN 222014846080", "AND", "252104", "732030"], None),            # breaks and AND, then pieces
    (["APN 222014846080", "AND", "", "252104732030"], ["222014846080", "252104732030"]),
], ids=["real", "2629393", "two-on-a-line", "two-lines", "colon-comma", "hash", "dashed",
        "nine-digit", "spaced-second", "spaced-only", "amp-split", "glued-amp", "nbsp",
        "apn-no-split", "extra-word", "trustee-word", "str-line", "no-apn", "no-numbers", "paren-pieces",
        "empty-apn-line", "bare-label", "additional-apn", "spaced-elsewhere",
        "short-numbers", "split-across-break", "inline-tags", "label-apart",
        "bare-line-pieces", "bare-line-apn",
        "and-pieces", "and-next-line",
        "and-own-line-pieces", "and-own-line-apn"])
def test_the_live_answer_is_trusted_only_when_whole_and_clean(monkeypatch, lines, expected):
    page = _page(*lines)
    monkeypatch.setattr(crawler, "_bounded_fetch", lambda *a, **k: page.encode())
    assert chelan_live_recording_apns("1") == expected


def test_a_line_outside_a_span_is_still_checked(monkeypatch):
    page = _DETAIL.replace(_LEGAL_BLOCK, '<span class="" >APN 222014846080</span><br />'
                           '<span class="" >LOT 7 AS DESCRIBED IN TRUSTEE&#39;S DEED</span><br />'
                           'APN 252104732030<br />')
    monkeypatch.setattr(crawler, "_bounded_fetch", lambda *a, **k: page.encode())
    assert chelan_live_recording_apns("1") is None  # the parser stops at TRUSTEE: never partial


def test_a_page_without_a_doclegal_block_is_unknown_unless_it_lists_nothing(monkeypatch):
    page = _DETAIL.replace("DocLegal:", "Other:")
    monkeypatch.setattr(crawler, "_bounded_fetch", lambda *a, **k: page.encode())
    assert chelan_live_recording_apns("1") in (None, [])
