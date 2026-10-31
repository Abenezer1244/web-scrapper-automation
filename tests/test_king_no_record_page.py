"""eRealProperty's own "no such parcel" answer, and how phase 1 labels it.

The pages below are verbatim slices of live eRealProperty responses fetched
2026-10-31 with the worker's URL and User-Agent: ParcelNbr=0000000001 (a PIN King
does not carry) and ParcelNbr=9906100000 (a real parcel whose name, site address and
legal are blank). The label is one proof of three; the settling itself is tested in
test_mailing_recovery_no_record.py.

No mocks of our own code. A tiny fake stands in for `requests`' Response, because
the alternative is asking King County for a missing parcel on cue.
"""
from __future__ import annotations

import asyncio

import pytest

from src.scrapers.enrichment import king_county_assessor as kca

NO_RECORD_PAGE = (
    '<span id="cphContent_LblMsg" style="color:Red;font-weight:bold;">No data found.</span><br /> '
    '<table cellpadding="0" cellspacing="0" class="_table2" > <tr class="_table_headerrow">'
    '<th style="height: 30px; vertical-align: middle;">PARCEL</th></tr> <tr> '
    '<td style="width: 750px"> <div> </div> </td> </tr>'
)

BLANK_REAL_PARCEL_PAGE = (
    '<span id="cphContent_LblMsg" style="color:Red;font-weight:bold;"></span><br /> '
    '<table cellpadding="0" cellspacing="0" class="_table2" > <tr class="_table_headerrow">'
    '<th style="height: 30px; vertical-align: middle;">PARCEL</th></tr> <tr> '
    '<td style="width: 750px"> <div> <table class="GridViewStyle" cellspacing="1" cellpadding="3" '
    'rules="all" border="1" id="cphContent_DetailsViewDashboardHeader" '
    'style="font-size:Small;height:50px;width:700px;"> <tr class="GridViewRowStyle"> '
    '<td style="font-weight:bold;">Parcel Number</td><td>990610-0000</td> </tr>'
    '<tr class="GridViewAlternatingRowStyle"> <td style="font-weight:bold;">Name</td><td>&nbsp;</td> </tr>'
    '<tr class="GridViewRowStyle"> <td style="font-weight:bold;">Site Address</td><td>&nbsp;</td> </tr>'
    '<tr class="GridViewAlternatingRowStyle"> <td style="font-weight:bold;">Legal</td><td>&nbsp;</td> </tr> '
    '</table> </div> </td> </tr>'
)


class TestIsNoRecordPage:
    def test_the_live_missing_parcel_answer_is_recognised(self):
        assert kca.is_no_record_page(NO_RECORD_PAGE)

    def test_a_real_parcel_with_blank_fields_is_not(self):
        assert not kca.is_no_record_page(BLANK_REAL_PARCEL_PAGE)

    @pytest.mark.parametrize("page", [
        "",                                                       # empty 200
        "<html><body>Service temporarily unavailable</body></html>",  # interstitial
        "<p>No data found.</p>",                                  # the words, not the label
        '<span id="cphContent_LblMsg">Please try again later.</span>',  # the label, other text
    ])
    def test_anything_short_of_the_exact_answer_is_not(self, page):
        assert not kca.is_no_record_page(page)

    def test_the_message_beside_a_rendered_parcel_is_not(self):
        # Belt for a future layout: the message alone never outweighs a parcel table.
        page = BLANK_REAL_PARCEL_PAGE.replace('font-weight:bold;"></span>',
                                              'font-weight:bold;">No data found.</span>')
        assert not kca.is_no_record_page(page)


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text
        self.url = "https://blue.kingcounty.com/x"
        self.headers = {}


@pytest.fixture
def offline(monkeypatch):
    """No Redis lease, no DB health gate, no durable block record, no real pacing."""
    class _Always:
        admitted = True

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def still_held(self):
            return True

    real_sleep = asyncio.sleep

    async def _noop(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _Always)
    monkeypatch.setattr(kca, "check_source_or_raise", lambda *_a, **_k: None)
    monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)
    monkeypatch.setattr(kca.asyncio, "sleep", _noop)


def test_phase_one_labels_the_answer_but_still_counts_and_defers_it(monkeypatch, offline):
    monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(200, NO_RECORD_PAGE))
    stats: dict = {}

    results = asyncio.run(kca.batch_enrich_king_county(
        ["9999990001"], stats=stats, do_mailing=False, pace_s=0.1))

    assert results["9999990001"]["parcel_lookup"] == "no_record"
    assert results["9999990001"]["property_address"] is None
    # Unchanged accounting: still a breaker failure, still deferred, no tax-bill URL.
    assert "HTTP200_no_parcel" in stats["phase1_outcomes"]
    assert stats["deferred"] == ["9999990001"]
    assert stats["mailing_candidates"] == 0


def test_an_unrecognised_no_echo_page_gets_no_label(monkeypatch, offline):
    monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(200, "<html>maintenance</html>"))
    stats: dict = {}

    results = asyncio.run(kca.batch_enrich_king_county(
        ["9999990001"], stats=stats, do_mailing=False, pace_s=0.1))

    assert "9999990001" not in results
    assert stats["deferred"] == ["9999990001"]
