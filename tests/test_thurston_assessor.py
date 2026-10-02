"""Thurston taxpayer mailing from the Assessor A+ parcel page.

Thurston is an EagleWeb county with no mailing source. These tests pin the page
parser (the live markup of 2026-10-02, names redacted) and the resolver's identity,
block and deferral behaviour. Real Redis lease, real source_health rows; only the
county's HTTP boundary is substituted.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import requests
from sqlalchemy import text

from src.scrapers.enrichment import thurston_assessor as ta
from src.scrapers.enrichment.snohomish_assessor_roll import FOUND, SOURCE_UNAVAILABLE

_FIXTURES = Path(__file__).parent / "fixtures"
_PN = "74700001201"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ta, "_PACE_S", 0.0)
    monkeypatch.setattr(ta, "_JITTER_S", 0.0)
    monkeypatch.setattr(ta, "_RETRY_BACKOFF_S", 0.0)
    from src.scrapers.enrichment import pacs_parcel as pp

    monkeypatch.setattr(pp, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(pp, "_JITTER_S", 0.0)


@pytest.fixture(autouse=True)
def _clean_source_state(redis_client):
    from src.db.session import system_sync_session

    def _reset():
        redis_client.delete(f"bl:source_admission:{ta.SOURCE}")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key = :k"), {"k": ta.SOURCE})
            s.commit()

    _reset()
    yield
    _reset()


def _page(pn: str, *, situs: str = "1418 COLLEGE ST SE",
          owner: list[str] | None = None, taxpayer: list[str] | None = None) -> str:
    """The A+ page in the live markup. A block is a list of [street, locality]
    rows; [] = the label with an empty address; None = no such block."""
    def block(label: str, lines: list[str] | None) -> str:
        if lines is None:
            return ""
        rows = f"<tr><td class='emphatic'>{label}:</td>\n<td>NAME</td></tr>\n"
        if lines:
            rows += f"<tr><td class='emphatic'>Address:</td><td>{lines[0]}</td></tr>\n"
            for extra in lines[1:]:
                rows += f"<tr><td>&nbsp;</td><td>{extra}</td></tr>\n"
        else:
            rows += "<tr><td class='emphatic'>Address:</td><td></td></tr>\n"
        return rows + "<tr><td colspan='2'>&nbsp;</td></tr>\n"
    return (f"<html><body><table><tr><td class='emphatic' colspan='2'>Parcel Number: {pn}</td></tr>\n"
            f"<tr><td class='emphatic' width='30%'>Situs Address:</td>\n<td>{situs}</td></tr>\n"
            f"<tr><td colspan='2'>&nbsp;</td></tr>\n{block('Owner', owner)}{block('Taxpayer', taxpayer)}"
            f"<tr><td class='emphatic'>Abbreviated Legal:</td><td>LOT 12</td></tr></table></body></html>")


class TestParsePage:
    def test_real_page_takes_the_taxpayer_address(self):
        answer = ta.parse_page((_FIXTURES / f"thurston_aplus_{_PN}.html").read_text(encoding="utf-8"), _PN)
        assert answer.outcome == FOUND
        assert answer.mailing_address == "3000 PACIFIC AVE SE, OLYMPIA, WA 98501"
        assert answer.role == "taxpayer"

    def test_taxpayer_beats_owner_when_they_differ(self):
        html = _page(_PN, owner=["1 OWNER ST", "OLYMPIA, WA 98501"], taxpayer=["PO BOX 9", "LACEY, WA 98503"])
        assert ta.parse_page(html, _PN).mailing_address == "PO BOX 9, LACEY, WA 98503"

    def test_owner_block_is_used_only_without_a_taxpayer_block(self):
        answer = ta.parse_page(_page(_PN, owner=["1 OWNER ST", "TUMWATER, WA 98512"]), _PN)
        assert answer.mailing_address == "1 OWNER ST, TUMWATER, WA 98512" and answer.role == "owner"

    def test_mailing_different_from_the_situs_out_of_state(self):
        answer = ta.parse_page(_page(_PN, taxpayer=["PO BOX 800", "PHOENIX, AZ 85001"]), _PN)
        assert answer.mailing_address == "PO BOX 800, PHOENIX, AZ 85001"

    def test_unit_is_kept(self):
        answer = ta.parse_page(_page(_PN, taxpayer=["12 MAIN ST APT 4B", "OLYMPIA, WA 98501"]), _PN)
        assert answer.mailing_address == "12 MAIN ST APT 4B, OLYMPIA, WA 98501"

    def test_empty_taxpayer_address_is_a_settled_none(self):
        assert ta.parse_page(_page(_PN, taxpayer=[]), _PN).outcome == ta.NONE

    def test_a_page_for_another_parcel_is_never_taken(self):
        answer = ta.parse_page(_page("74700001299", taxpayer=["PO BOX 9", "LACEY, WA 98503"]), _PN)
        assert answer.outcome == ta.PARCEL_MISMATCH and answer.mailing_address is None

    def test_leading_zeros_do_not_break_identity(self):
        html = _page("0" + _PN, taxpayer=["PO BOX 9", "LACEY, WA 98503"])
        assert ta.parse_page(html, _PN).is_found

    def test_no_owner_or_taxpayer_block_is_unparsed(self):
        assert ta.parse_page(_page(_PN), _PN).outcome == ta.UNPARSED

    def test_a_locality_only_block_is_unparsed_not_none(self):
        html = _page(_PN, taxpayer=["", "OLYMPIA, WA 98501"]).replace("<td></td>", "<td>OLYMPIA, WA 98501</td>")
        assert ta.parse_page(html, _PN).outcome == ta.UNPARSED

    @pytest.mark.parametrize("body", ["", "<html><body>Server busy</body></html>"])
    def test_unreadable_pages_are_unparsed(self, body):
        assert ta.parse_page(body, _PN).outcome == ta.UNPARSED

    def test_the_sites_no_record_page_is_not_found(self):
        assert ta.parse_page("<html><body>No records were found for that parcel.</body></html>", _PN).outcome == ta.PARCEL_NOT_FOUND


# ─── Resolver ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body


def _serve(monkeypatch, pages: dict):
    asked: list[str] = []

    def _get(session, parcel_key):
        asked.append(parcel_key)
        answer = pages[parcel_key]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return _Resp(answer)
        return _Resp(200, answer)

    monkeypatch.setattr(ta, "_http_get", _get)
    return asked


_GOOD = _page(_PN, taxpayer=["PO BOX 800", "PHOENIX, AZ 85001"])


class TestResolve:
    def test_found_settled_and_deferred(self, monkeypatch):
        other, third = "74700001202", "74700001203"
        _serve(monkeypatch, {_PN: _GOOD, other: _page(other, taxpayer=[]),
                             third: "<html>No records were found</html>"})
        out = ta.resolve_mailing([_PN, other, third])
        assert out[_PN].mailing_address == "PO BOX 800, PHOENIX, AZ 85001"
        assert out[other].outcome == ta.NONE
        assert out[third].outcome == ta.PARCEL_NOT_FOUND

    def test_answers_every_caller_spelling_with_one_request(self, monkeypatch):
        """One request, sent in the longest (zero-kept) spelling; every caller answered."""
        asked = _serve(monkeypatch, {"0" + _PN: _GOOD})
        out = ta.resolve_mailing([_PN, "0" + _PN, "747-00001201"])
        assert all(a.is_found for a in out.values()) and asked == ["0" + _PN]

    def test_a_malformed_parcel_is_never_requested(self, monkeypatch):
        asked = _serve(monkeypatch, {})
        assert {a.outcome for a in ta.resolve_mailing(["", "ABC"]).values()} == {ta.PARCEL_NOT_FOUND}
        assert asked == []

    def test_a_transient_timeout_is_retried_once(self, monkeypatch):
        asked = _serve(monkeypatch, {_PN: [requests.Timeout(), _GOOD]})
        assert ta.resolve_mailing([_PN])[_PN].is_found and asked == [_PN, _PN]

    @pytest.mark.parametrize("failure", [[503, 503], [requests.ConnectionError(), requests.ConnectionError()]])
    def test_a_failure_that_survives_its_retry_is_request_failed(self, monkeypatch, failure):
        other = "74700001202"
        _serve(monkeypatch, {_PN: failure, other: _page(other, taxpayer=[])})
        out = ta.resolve_mailing([_PN, other])
        assert out[_PN].outcome == ta.REQUEST_FAILED and out[other].outcome == ta.NONE

    @pytest.mark.parametrize("status", [429, 403])
    def test_a_block_stops_the_pass_and_cools_the_source(self, monkeypatch, status):
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_health import is_source_available

        other = "74700001202"
        asked = _serve(monkeypatch, {_PN: status, other: _GOOD})
        out = ta.resolve_mailing([_PN, other])
        assert {out[_PN].outcome, out[other].outcome} == {SOURCE_UNAVAILABLE}
        assert asked == [_PN]
        with system_sync_session() as s:
            assert is_source_available(s, ta.SOURCE) is False
        asked2 = _serve(monkeypatch, {_PN: _GOOD})
        assert ta.resolve_mailing([_PN])[_PN].outcome == SOURCE_UNAVAILABLE and asked2 == []

    def test_layout_drift_trips_the_breaker(self, monkeypatch):
        pids = [f"7470000120{i}" for i in range(6)]
        asked = _serve(monkeypatch, dict.fromkeys(pids, "<html><body>new layout</body></html>"))
        out = ta.resolve_mailing(pids)
        assert len(asked) == ta._UNPARSED_STREAK_LIMIT
        assert {out[p].outcome for p in pids[3:]} == {SOURCE_UNAVAILABLE}

    def test_no_lease_no_request(self, monkeypatch, redis_client):
        monkeypatch.setattr(ta, "_LEASE_WAIT_S", 0.0)
        redis_client.set(f"bl:source_admission:{ta.SOURCE}", "someone-else", ex=60)
        asked = _serve(monkeypatch, {_PN: _GOOD})
        assert ta.resolve_mailing([_PN])[_PN].outcome == SOURCE_UNAVAILABLE and asked == []

    def test_the_budget_defers_what_it_cannot_reach(self, monkeypatch):
        a, b = _PN, "74700001202"
        _serve(monkeypatch, {a: _GOOD, b: _GOOD})
        monkeypatch.setattr(ta, "_TIMEOUT_S", 5)
        out = ta.resolve_mailing([a, b], time_budget_s=4.0)
        assert out[a].is_found and out[b].outcome == SOURCE_UNAVAILABLE
