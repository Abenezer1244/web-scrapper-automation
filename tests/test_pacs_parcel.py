"""Parcel-keyed owner mailing from Tyler/Harris PACS portals (Benton first).

Benton probate had 0/7 mailing addresses (admin job bc8d507c) while the county's
PACS portal shows one for the sampled parcel. These tests pin the chain that closes
identity twice (grid Geo ID cell, then the detail page's own identity cell) before
any address is taken, and the resolver's pacing, block and deferral behaviour.

Real Redis lease, real source_health rows. Only the county's HTTP boundary is
substituted, with pages built from the exact markup Benton served on 2026-10-02
(owner name redacted in the saved fixtures).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import requests
from sqlalchemy import text

from src.scrapers.enrichment import pacs_parcel as pp
from src.scrapers.enrichment.snohomish_assessor_roll import AMBIGUOUS, FOUND, SOURCE_UNAVAILABLE

_FIXTURES = Path(__file__).parent / "fixtures"
_PARCEL = "131073011125003"
_SITE = pp.PACS_SITES["benton"]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
    monkeypatch.setattr(pp, "_PACE_S", 0.0)
    monkeypatch.setattr(pp, "_JITTER_S", 0.0)
    monkeypatch.setattr(pp, "_RETRY_BACKOFF_S", 0.0)


@pytest.fixture(autouse=True)
def _clean_source_state(redis_client):
    from src.db.session import system_sync_session

    def _reset():
        for site in pp.PACS_SITES.values():
            redis_client.delete(f"bl:source_admission:{site.source_key}")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key LIKE 'pacs_%'"))
            s.commit()

    _reset()
    yield
    _reset()


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _results(rows: list[tuple[str, str]]) -> str:
    """A results grid in Benton's column order. rows = [(geo_id, prop_id), ...]."""
    body = "".join(
        f"<tr><td><input type=checkbox></td><td>{prop}</td><td>{geo}</td><td>Real</td><td>1 - 1212</td>"
        f"<td>65003 N SR 225<br>BENTON CITY, WA 99320</td><td>SHORT PLAT</td><td>OWNER</td><td>$1</td>"
        f'<td><a href="Property.aspx?cid=0&prop_id={prop}&year=2026">View Details</a></td></tr>'
        for geo, prop in rows)
    return (f"<html><body><table id='propertySearchResults_resultsTable'><tr><th></th><th>Property ID</th>"
            f"<th>Parcel # / Geo ID</th><th>Type</th><th>Tax Area</th><th>Property Address</th>"
            f"<th>Legal Description</th><th>Owner Name</th><th>Appraised Value</th><th></th></tr>"
            f"{body}</table></body></html>")


_NONE_FOUND = "<html><body><div id='propertySearchResults'>None found.</div></body></html>"


def _detail(mailing_cells: list[str], *, geo: str = _PARCEL) -> str:
    owners = "".join(
        f"<tr><td>Name:</td><td>OWNER</td><td>Owner ID:</td><td>1</td></tr>"
        f"<tr><td>Mailing Address:</td><td>{cell}</td><td>% Ownership:</td><td>50%</td></tr>"
        for cell in mailing_cells)
    return (f"<html><body><table><tr><td>Property ID:</td><td>58808</td></tr>"
            f"<tr><td>Parcel # / Geo ID:</td><td>{geo}</td><td>Agent Code:</td><td></td></tr>"
            f"<tr><td>Address:</td><td>65003 N SR 225 <BR> BENTON CITY, WA 99320</td><td>Mapsco:</td><td></td></tr>"
            f"{owners}</table></body></html>")


# ─── Parsers over the live markup ────────────────────────────────────────────

class TestParseResults:
    def test_real_benton_grid_resolves_the_one_matching_row(self):
        assert pp.parse_results(_fixture(f"benton_pacs_results_{_PARCEL}.html"), _PARCEL) == (FOUND, "58808")

    def test_rows_for_other_parcels_only_is_not_found(self):
        assert pp.parse_results(_results([("131073011125999", "1")]), _PARCEL) == (pp.PARCEL_NOT_FOUND, None)

    def test_two_rows_for_our_parcel_is_ambiguous(self):
        assert pp.parse_results(_results([(_PARCEL, "1"), (_PARCEL, "2")]), _PARCEL) == (AMBIGUOUS, None)

    def test_a_matching_row_among_strangers_is_still_found(self):
        html = _results([("131073011125999", "1"), (_PARCEL, "58808"), ("131073011125001", "3")])
        assert pp.parse_results(html, _PARCEL) == (FOUND, "58808")

    def test_none_found_page(self):
        assert pp.parse_results(_NONE_FOUND, _PARCEL) == (pp.PARCEL_NOT_FOUND, None)

    def test_a_page_without_the_grid_is_unparsed_not_not_found(self):
        assert pp.parse_results("<html><body>Maintenance</body></html>", _PARCEL) == (pp.UNPARSED, None)

    def test_leading_zeros_and_hyphens_in_the_county_cell_still_match(self):
        html = _results([("0" + _PARCEL[:6] + "-" + _PARCEL[6:], "58808")])
        assert pp.parse_results(html, _PARCEL) == (FOUND, "58808")


class TestParseDetail:
    def test_real_benton_detail_page(self):
        answer = pp.parse_detail(_fixture("benton_pacs_detail_58808.html"), _PARCEL)
        assert answer.outcome == FOUND
        assert answer.mailing_address == "65003 N SR 225, BENTON CITY, WA 99320"
        assert answer.role == "owner"

    def test_mailing_different_from_the_property(self):
        answer = pp.parse_detail(_detail(["PO BOX 800 <BR> PHOENIX, AZ 85001"]), _PARCEL)
        assert answer.is_found and answer.mailing_address == "PO BOX 800, PHOENIX, AZ 85001"

    def test_unit_is_preserved_as_the_county_printed_it(self):
        answer = pp.parse_detail(_detail(["4603-T NE 85TH ST <BR> VANCOUVER, WA 98665"]), _PARCEL)
        assert answer.mailing_address == "4603-T NE 85TH ST, VANCOUVER, WA 98665"

    def test_addressee_lines_before_the_street_are_dropped(self):
        answer = pp.parse_detail(_detail(["DOE FAMILY TRUST <BR> C/O JANE DOE <BR> 9 PINE RD <BR> LANGLEY, WA 98260"]), _PARCEL)
        assert answer.mailing_address == "9 PINE RD, LANGLEY, WA 98260"

    def test_foreign_mailing_keeps_its_country(self):
        answer = pp.parse_detail(_detail(["12 RUE DE LA PAIX <BR> 75002 PARIS <BR> FRANCE"]), _PARCEL)
        assert answer.mailing_address == "12 RUE DE LA PAIX, 75002 PARIS, FRANCE"

    def test_empty_mailing_cell_is_a_settled_none(self):
        answer = pp.parse_detail(_detail([""]), _PARCEL)
        assert answer.outcome == pp.NONE and answer.mailing_address is None

    def test_co_owners_with_the_same_address_are_one_answer(self):
        html = _detail(["9 PINE RD <BR> LANGLEY, WA 98260", "9 PINE RD <BR> LANGLEY, WA 98260"])
        assert pp.parse_detail(html, _PARCEL).mailing_address == "9 PINE RD, LANGLEY, WA 98260"

    def test_co_owners_with_different_addresses_are_ambiguous(self):
        html = _detail(["9 PINE RD <BR> LANGLEY, WA 98260", "PO BOX 1 <BR> LANGLEY, WA 98260"])
        assert pp.parse_detail(html, _PARCEL).outcome == AMBIGUOUS

    def test_a_page_for_another_parcel_is_never_taken(self):
        answer = pp.parse_detail(_detail(["9 PINE RD <BR> LANGLEY, WA 98260"], geo="131073011125999"), _PARCEL)
        assert answer.outcome == pp.PARCEL_MISMATCH and answer.mailing_address is None

    def test_a_page_naming_two_parcels_is_a_mismatch(self):
        html = _detail(["9 PINE RD <BR> LANGLEY, WA 98260"]).replace(
            "<tr><td>Address:", f"<tr><td>Geographic ID:</td><td>{_PARCEL}</td></tr><tr><td>Address:")
        assert pp.parse_detail(html, _PARCEL).outcome == pp.PARCEL_MISMATCH

    def test_no_owner_block_at_all_is_unparsed(self):
        assert pp.parse_detail(_detail([]), _PARCEL).outcome == pp.UNPARSED

    def test_an_addressee_only_cell_is_unparsed_not_none(self):
        assert pp.parse_detail(_detail(["DOE FAMILY TRUST"]), _PARCEL).outcome == pp.UNPARSED

    @pytest.mark.parametrize("body", ["", "<html><body>503 Service Unavailable</body></html>"])
    def test_unreadable_pages_are_unparsed(self, body):
        assert pp.parse_detail(body, _PARCEL).outcome == pp.UNPARSED


# ─── Resolver: pacing, identity, blocks, deferral ────────────────────────────

class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body


_FORM = _fixture("benton_pacs_search_form.html")


def _serve(monkeypatch, search: dict, detail: dict, form=None):
    """The portal answers: form GET, search POST by geoid, detail GET by prop_id.
    Values: body | status | exception | list of those (consumed in order)."""
    log: list[tuple[str, str]] = []

    def _take(answer):
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return _Resp(answer)
        return _Resp(200, answer)

    def _get(session, url):
        if url == _SITE.search_url:
            log.append(("form", ""))
            return _take(_FORM if form is None else form)
        pid = url.rsplit("prop_id=", 1)[1]
        log.append(("detail", pid))
        return _take(detail[pid])

    def _post(session, url, data):
        assert url == _SITE.search_url
        assert data["__VIEWSTATE"]  # the ASP.NET tokens from the form are echoed
        geo = data["propertySearchOptions$geoid"]
        log.append(("search", geo))
        return _take(search[geo])

    monkeypatch.setattr(pp, "_http_get", _get)
    monkeypatch.setattr(pp, "_http_post", _post)
    return log


_GOOD_SEARCH = _results([(_PARCEL, "58808")])
_GOOD_DETAIL = _detail(["PO BOX 800 <BR> PHOENIX, AZ 85001"])


class TestResolve:
    def test_found_settled_and_deferred_are_three_different_things(self, monkeypatch):
        other, third = "131073011125001", "131073011125002"
        _serve(monkeypatch,
               search={_PARCEL: _GOOD_SEARCH, other: _NONE_FOUND, third: _results([(third, "7")])},
               detail={"58808": _GOOD_DETAIL, "7": _detail([""], geo=third)})
        out = pp.resolve_mailing("benton", [_PARCEL, other, third])
        assert out[_PARCEL].is_found and out[_PARCEL].mailing_address == "PO BOX 800, PHOENIX, AZ 85001"
        assert out[other].outcome == pp.PARCEL_NOT_FOUND
        assert out[third].outcome == pp.NONE

    def test_answers_every_caller_spelling_with_one_lookup(self, monkeypatch):
        log = _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _GOOD_DETAIL})
        out = pp.resolve_mailing("benton", [_PARCEL, "0" + _PARCEL, _PARCEL[:6] + "-" + _PARCEL[6:]])
        assert all(a.is_found for a in out.values()) and len(out) == 3
        assert [e for e in log if e[0] == "search"] == [("search", _PARCEL)]

    def test_a_detail_page_for_another_parcel_is_a_mismatch_not_an_address(self, monkeypatch):
        _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _detail(["9 PINE RD <BR> LANGLEY, WA 98260"], geo="1")})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].outcome == pp.PARCEL_MISMATCH

    def test_a_malformed_parcel_is_never_requested(self, monkeypatch):
        log = _serve(monkeypatch, {}, {})
        out = pp.resolve_mailing("benton", ["", "ABC", "0000"])
        assert {a.outcome for a in out.values()} == {pp.PARCEL_NOT_FOUND}
        assert log == []

    def test_an_unknown_county_makes_no_request(self, monkeypatch):
        log = _serve(monkeypatch, {}, {})
        out = pp.resolve_mailing("pierce", [_PARCEL])
        assert out[_PARCEL].outcome == SOURCE_UNAVAILABLE and log == []

    def test_kill_switch_off_holds_a_restricted_county_but_not_benton(self, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
        log = _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _GOOD_DETAIL})
        assert pp.resolve_mailing("whatcom", [_PARCEL])[_PARCEL].outcome == SOURCE_UNAVAILABLE
        assert log == []
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].is_found

    def test_a_transient_timeout_is_retried_once(self, monkeypatch):
        log = _serve(monkeypatch, {_PARCEL: [requests.Timeout(), _GOOD_SEARCH]}, {"58808": _GOOD_DETAIL})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].is_found
        assert [e for e in log if e[0] == "search"] == [("search", _PARCEL)] * 2

    def test_a_5xx_that_recovers_on_retry_is_an_answer(self, monkeypatch):
        _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": [503, _GOOD_DETAIL]})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].is_found

    @pytest.mark.parametrize("failure", [[503, 503], [requests.ConnectionError(), requests.ConnectionError()]])
    def test_a_failure_that_survives_its_retry_is_request_failed(self, monkeypatch, failure):
        other = "131073011125001"
        _serve(monkeypatch, {_PARCEL: failure, other: _NONE_FOUND}, {})
        out = pp.resolve_mailing("benton", [_PARCEL, other])
        assert out[_PARCEL].outcome == pp.REQUEST_FAILED
        assert out[other].outcome == pp.PARCEL_NOT_FOUND  # the pass goes on

    def test_a_client_error_is_not_retried(self, monkeypatch):
        log = _serve(monkeypatch, {_PARCEL: 404}, {})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].outcome == pp.REQUEST_FAILED
        assert len([e for e in log if e[0] == "search"]) == 1

    @pytest.mark.parametrize("status", [429, 403])
    def test_a_block_stops_the_pass_and_cools_the_source_for_everyone(self, monkeypatch, status):
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_health import is_source_available

        other = "131073011125001"
        log = _serve(monkeypatch, {_PARCEL: status, other: _NONE_FOUND}, {})
        out = pp.resolve_mailing("benton", [_PARCEL, other])
        # The county refused: nothing is known about either parcel, both defer and
        # the cooldown (not a per-parcel attempt) decides when they are asked again.
        assert out[_PARCEL].outcome == SOURCE_UNAVAILABLE
        assert out[other].outcome == SOURCE_UNAVAILABLE  # never asked
        assert ("search", other) not in log
        with system_sync_session() as s:
            assert is_source_available(s, "pacs_benton") is False
        # The next pass honours the cooldown without a request.
        log2 = _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _GOOD_DETAIL})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].outcome == SOURCE_UNAVAILABLE
        assert log2 == []

    def test_a_block_on_one_county_does_not_cool_another(self, monkeypatch):
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_health import is_source_available

        _serve(monkeypatch, {_PARCEL: 429}, {})
        pp.resolve_mailing("benton", [_PARCEL])
        with system_sync_session() as s:
            assert is_source_available(s, "pacs_jefferson") is True

    def test_layout_drift_trips_the_breaker_instead_of_burning_every_parcel(self, monkeypatch):
        pids = [f"13107301112500{i}" for i in range(6)]
        log = _serve(monkeypatch, dict.fromkeys(pids, "<html><body>new layout</body></html>"), {})
        out = pp.resolve_mailing("benton", pids)
        assert len([e for e in log if e[0] == "search"]) == pp._UNPARSED_STREAK_LIMIT
        assert {out[p].outcome for p in pids[:3]} == {pp.UNPARSED}
        assert {out[p].outcome for p in pids[3:]} == {SOURCE_UNAVAILABLE}

    def test_an_unreadable_search_form_defers_everything(self, monkeypatch):
        log = _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {}, form="<html><body>offline for maintenance</body></html>")
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].outcome == SOURCE_UNAVAILABLE
        assert log == [("form", "")]

    def test_no_lease_no_request(self, monkeypatch, redis_client):
        monkeypatch.setattr(pp, "_LEASE_WAIT_S", 0.0)
        redis_client.set("bl:source_admission:pacs_benton", "someone-else", ex=60)
        log = _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _GOOD_DETAIL})
        assert pp.resolve_mailing("benton", [_PARCEL])[_PARCEL].outcome == SOURCE_UNAVAILABLE
        assert log == []

    def test_the_budget_defers_what_it_cannot_reach(self, monkeypatch):
        a, b = _PARCEL, "131073011125001"
        _serve(monkeypatch, {a: _NONE_FOUND, b: _NONE_FOUND}, {})
        monkeypatch.setattr(pp, "_TIMEOUT_S", 5)  # reserve = 2 * 5 s with pacing at 0
        out = pp.resolve_mailing("benton", [a, b], time_budget_s=8.0)
        assert out[a].outcome == pp.PARCEL_NOT_FOUND
        assert out[b].outcome == SOURCE_UNAVAILABLE

    def test_every_request_is_paced_including_the_first(self, monkeypatch):
        events: list[str] = []
        monkeypatch.setattr(pp, "_PACE_S", 3.0)
        monkeypatch.setattr(pp.time, "sleep", lambda s: events.append(f"sleep {s:g}"))
        _serve(monkeypatch, {_PARCEL: _GOOD_SEARCH}, {"58808": _GOOD_DETAIL})
        monkeypatch.setattr(pp, "_http_get", lambda session, url: (events.append("get"), _Resp(200, _FORM if url == _SITE.search_url else _GOOD_DETAIL))[1])
        monkeypatch.setattr(pp, "_http_post", lambda session, url, data: (events.append("post"), _Resp(200, _GOOD_SEARCH))[1])
        pp.resolve_mailing("benton", [_PARCEL])
        assert events == ["sleep 3", "get", "sleep 3", "post", "sleep 3", "get"]
