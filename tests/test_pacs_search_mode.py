"""PACS searches must say WHICH search they are: the portal's search-mode field.

Seen live 2026-10-31 (`pacs_clallam: 3 unparsed pages in a row; layout drift`): the
parcel postback never sent `propertySearchOptions$searchType`. Clallam, Grant,
Jefferson, Chelan and Island answered every parcel search with the empty form and
"You must enter search criteria."; only Benton (no owner mode) worked. Clallam and
Island also head the parcel column "Geographic ID", which the results parser did not
recognise. The owner-name path had the same omission.

The fixtures are the live pages those portals served (owner, address, legal and value
cells redacted). The fake portal below answers exactly as they did: a form with a mode
dropdown and a postback without a mode gets the "must enter search criteria" page.
Real Redis lease and real source_health rows; only the county's HTTP boundary is
substituted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from src.scrapers.enrichment import pacs as pacs_mod
from src.scrapers.enrichment import pacs_parcel as pp
from src.scrapers.enrichment.snohomish_assessor_roll import AMBIGUOUS, FOUND, SOURCE_UNAVAILABLE

_FIXTURES = Path(__file__).parent / "fixtures"
_CLALLAM_GEO = "0630115504300000"
_CLALLAM_SHORT = "063011550430"     # the 12-digit spelling another source carries


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


_CLALLAM_FORM = _fixture("clallam_pacs_search_form.html")
_CLALLAM_CRITERIA_ERROR = _fixture("clallam_pacs_search_criteria_error.html")
_CLALLAM_RESULTS = _fixture(f"clallam_pacs_results_{_CLALLAM_GEO}.html")
_GRANT_FORM = _fixture("grant_pacs_search_criteria_error.html")   # the form, owner mode selected


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body
        self.headers: dict = {}


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


def _clallam_portal(monkeypatch, *, form: str | int = _CLALLAM_FORM, detail: str | None = None,
                    results: str = _CLALLAM_RESULTS, query: str = _CLALLAM_GEO) -> list:
    """Clallam as it behaved live: it reads the parcel field only in account-number mode."""
    site = pp.PACS_SITES["clallam"]
    posts: list[dict] = []

    def _get(session, url):
        if url == site.search_url:
            return _Resp(form) if isinstance(form, int) else _Resp(200, form)
        return _Resp(200, detail or "")

    def _post(session, url, data):
        posts.append(dict(data))
        if data.get("propertySearchOptions$searchType") != "account-number":
            return _Resp(200, _CLALLAM_CRITERIA_ERROR)
        assert data["propertySearchOptions$geoid"] == query
        return _Resp(200, results)

    monkeypatch.setattr(pp, "_http_get", _get)
    monkeypatch.setattr(pp, "_http_post", _post)
    return posts


def _clallam_detail(geo: str = _CLALLAM_GEO) -> str:
    return ("<html><body><table><tr><td>Property ID:</td><td>65460</td></tr>"
            f"<tr><td>Geographic ID:</td><td>{geo}</td></tr>"
            "<tr><td>Name:</td><td>OWNER</td><td>Owner ID:</td><td>1</td></tr>"
            "<tr><td>Mailing Address:</td><td>PO BOX 800 <BR> PORT ANGELES, WA 98362</td>"
            "<td>% Ownership:</td><td>100%</td></tr></table></body></html>")


# ── the mode field, read from each live form ──────────────────────────────────

class TestSearchTypeField:
    def test_clallam_and_grant_forms_offer_both_searches(self):
        for form in (_CLALLAM_FORM, _GRANT_FORM):
            assert pacs_mod.search_type_field(form, "account-number") == {
                "propertySearchOptions$searchType": "account-number"}
            assert pacs_mod.search_type_field(form, "owner") == {
                "propertySearchOptions$searchType": "owner"}

    def test_benton_has_no_owner_search_so_a_name_search_is_refused(self):
        benton = _fixture("benton_pacs_search_form.html")
        assert pacs_mod.search_type_field(benton, "account-number") is not None
        assert pacs_mod.search_type_field(benton, "owner") is None

    def test_a_form_without_the_dropdown_needs_no_mode(self):
        assert pacs_mod.search_type_field("<form><input name='propertySearchOptions$geoid'></form>",
                                          "account-number") == {}


# ── the results page ──────────────────────────────────────────────────────────

class TestParseResults:
    def test_the_portals_criteria_error_is_unparsed_never_an_answer(self):
        assert pp.parse_results(_CLALLAM_CRITERIA_ERROR, "630115504300000")[:2] == (pp.UNPARSED, None)

    def test_a_geographic_id_column_is_read(self):
        assert pp.parse_results(_CLALLAM_RESULTS, "630115504300000")[:2] == (FOUND, "65460")

    def test_a_row_that_only_extends_our_id_is_ambiguous_not_not_found(self):
        # The 12-digit spelling finds the 16-digit row by the portal's prefix search.
        # Which one the lead means is unproven: charged and capped, never settled absent.
        assert pp.parse_results(_CLALLAM_RESULTS, "63011550430")[:2] == (AMBIGUOUS, None)

    def test_an_unrelated_row_is_still_not_found(self):
        # The live page's header row (<th> only) and its one-cell "Page: 1" footer are
        # not data rows, so they do not make this page unreadable.
        assert pp.parse_results(_CLALLAM_RESULTS, "730000000000000")[:2] == (pp.PARCEL_NOT_FOUND, None)

    def test_a_truncated_data_row_is_unparsed_not_not_found(self):
        page = _CLALLAM_RESULTS.replace(
            "</table>", "<tr><td><input type=checkbox></td><td>99999</td></tr></table>", 1)
        assert pp.parse_results(page, "730000000000000")[:2] == (pp.UNPARSED, None)

    def test_an_unreadable_identity_cell_is_unparsed(self):
        page = _CLALLAM_RESULTS.replace(_CLALLAM_GEO, "&nbsp;")
        assert pp.parse_results(page, "630115504300000")[:2] == (pp.UNPARSED, None)


# ── Clallam's 12-digit ids: the land row, and only when it is provably the only one ──
#
# Live 2026-10-31: the statewide layer carries `063011550430`; the portal prints
# `0630115504300000`. The county's own parcel layer maps each 12-digit GEO_12 to one
# PROP_ID, which was the portal's `id + "0000"` row in 33 of 33 parcels checked. Other
# suffixes exist (`053002300000` holds `...1000` and `...2001`, no `0000`), so any
# second row extending the id, or a result set that runs past this page, keeps it
# ambiguous.

_SHORT_KEY = "63011550430"                                     # the zero-stripped key
_LAND_ID = _CLALLAM_SHORT + "0000"
_ROW = re.search(r'(<td class="screenOnly"><span prop_id="65460">.*?)</tr>', _CLALLAM_RESULTS, re.S).group(1)


def _grid(rows: list[tuple[str, str]], *, heading: str | None = "auto", paging: str = "") -> str:
    """The live Clallam grid holding `rows` = [(geo_id, prop_id), ...]."""
    body = "</tr><tr>".join(_ROW.replace("65460", prop).replace(_CLALLAM_GEO, geo) for geo, prop in rows)
    page = _CLALLAM_RESULTS.replace(_ROW, body)
    if heading == "auto":
        heading = f"1 - {len(rows)} of {len(rows)}"
    page = page.replace("1 - 1 of 1", heading) if heading is not None else re.sub(
        r'<span id="propertySearchResults_pageHeading">.*?</span>', "", page, flags=re.S)
    # The live cell reads "Page:&nbsp;&nbsp;&nbsp;1": anchor on the cell, not its spaces.
    return re.sub(r'(<td class="paging"[^>]*>.*?)(</td>)', lambda m: m[1] + paging + m[2], page,
                  count=1, flags=re.S)


def _land(page: str, land_id: str | None = _LAND_ID):
    return pp.parse_results(page, _SHORT_KEY, land_id)


class TestLandId:
    def test_only_a_twelve_digit_clallam_spelling_names_a_land_row(self):
        clallam = pp.PACS_SITES["clallam"]
        assert clallam.land_id(_CLALLAM_SHORT) == _CLALLAM_GEO
        assert clallam.land_id(_SHORT_KEY) is None            # 11 digits: never padded
        assert clallam.land_id(_CLALLAM_GEO) is None          # already the full id
        assert clallam.land_id("15282810063a") is None
        assert clallam.land_id("") is None

    def test_no_other_county_has_the_equivalence(self):
        for county, site in pp.PACS_SITES.items():
            if county != "clallam":
                assert site.land_id(_CLALLAM_SHORT) is None, county


class TestParseLandRow:
    def test_the_only_row_extending_the_id_is_ours_and_the_detail_must_name_it(self):
        assert _land(_CLALLAM_RESULTS) == (FOUND, "65460", "630115504300000")

    def test_an_unrelated_row_beside_it_does_not_matter(self):
        assert _land(_grid([(_LAND_ID, "65460"), ("0730000000000000", "7")])) == (
            FOUND, "65460", "630115504300000")

    @pytest.mark.parametrize("other", ["0630115504301000", "0630115504302001", _LAND_ID])
    def test_a_second_row_extending_the_id_is_ambiguous(self, other):
        assert _land(_grid([(_LAND_ID, "65460"), (other, "7")]))[:2] == (AMBIGUOUS, None)

    def test_other_suffixes_without_a_land_row_stay_ambiguous(self):
        page = _grid([("0630115504301000", "1"), ("0630115504302001", "2")])
        assert _land(page)[:2] == (AMBIGUOUS, None)

    def test_a_result_set_that_runs_past_this_page_is_ambiguous(self):
        more = '<a href="SearchResults.aspx?cid=0&amp;rtype=address&amp;page=2">2</a>'
        assert _land(_grid([(_LAND_ID, "65460")], heading="1 - 1 of 2", paging=more))[:2] == (
            AMBIGUOUS, None)
        assert _land(_grid([(_LAND_ID, "65460")], heading="1 - 1 of 2"))[:2] == (AMBIGUOUS, None)
        assert _land(_grid([(_LAND_ID, "65460")], paging=more))[:2] == (AMBIGUOUS, None)

    def test_a_heading_that_disagrees_with_the_rows_read_is_not_proof(self):
        page = _grid([(_LAND_ID, "65460"), ("0730000000000000", "7")], heading="1 - 1 of 1")
        assert _land(page)[:2] == (AMBIGUOUS, None)

    def test_a_page_that_does_not_say_whether_it_is_complete_is_unparsed(self):
        assert _land(_grid([(_LAND_ID, "65460")], heading=None))[:2] == (pp.UNPARSED, None)

    def test_an_unreadable_neighbour_is_unparsed(self):
        page = _grid([(_LAND_ID, "65460"), ("&nbsp;", "7")])
        assert _land(page)[:2] == (pp.UNPARSED, None)

    def test_a_land_row_without_a_detail_link_is_unparsed(self):
        page = _CLALLAM_RESULTS.replace("prop_id=65460", "")
        assert _land(page)[:2] == (pp.UNPARSED, None)

    def test_zeros_are_compared_kept_so_a_stripped_spelling_is_not_the_land_row(self):
        # `630115504300000` strips to the same key as the land id, but it is not the
        # 16-digit id the equivalence is about: it only extends ours, so ambiguous.
        assert _land(_grid([("630115504300000", "65460")]))[:2] == (AMBIGUOUS, None)

    def test_without_a_land_id_nothing_changes(self):
        assert _land(_CLALLAM_RESULTS, None)[:2] == (AMBIGUOUS, None)


# ── the whole lookup against the portal as it behaved ─────────────────────────

class TestResolveClallam:
    def test_the_search_carries_its_mode_and_finds_the_mailing_address(self, monkeypatch):
        posts = _clallam_portal(monkeypatch, detail=_clallam_detail())

        out = pp.resolve_mailing("clallam", [_CLALLAM_GEO])

        assert out[_CLALLAM_GEO].outcome == FOUND
        assert out[_CLALLAM_GEO].mailing_address == "PO BOX 800, PORT ANGELES, WA 98362"
        assert posts and posts[0]["propertySearchOptions$searchType"] == "account-number"

    def test_the_statewide_twelve_digit_id_finds_the_mailing_address(self, monkeypatch):
        posts = _clallam_portal(monkeypatch, detail=_clallam_detail(), query=_CLALLAM_SHORT)

        out = pp.resolve_mailing("clallam", [_CLALLAM_SHORT])

        assert out[_CLALLAM_SHORT].outcome == FOUND
        assert out[_CLALLAM_SHORT].mailing_address == "PO BOX 800, PORT ANGELES, WA 98362"
        assert [p["propertySearchOptions$geoid"] for p in posts] == [_CLALLAM_SHORT]

    @pytest.mark.parametrize("named", [_CLALLAM_SHORT, "0630115504301000"])
    def test_a_detail_page_that_does_not_name_the_land_row_is_a_mismatch(self, monkeypatch, named):
        _clallam_portal(monkeypatch, detail=_clallam_detail(named), query=_CLALLAM_SHORT)

        assert pp.resolve_mailing("clallam", [_CLALLAM_SHORT])[_CLALLAM_SHORT].outcome == pp.PARCEL_MISMATCH

    def test_an_exact_grid_match_is_never_closed_by_a_land_detail_page(self, monkeypatch):
        exact = _grid([(_CLALLAM_SHORT, "65460")])
        _clallam_portal(monkeypatch, detail=_clallam_detail(_LAND_ID), results=exact, query=_CLALLAM_SHORT)

        assert pp.resolve_mailing("clallam", [_CLALLAM_SHORT])[_CLALLAM_SHORT].outcome == pp.PARCEL_MISMATCH

    def test_a_short_id_on_a_base_with_other_suffixes_stays_ambiguous(self, monkeypatch):
        page = _grid([(_LAND_ID, "65460"), ("0630115504301000", "7")])
        _clallam_portal(monkeypatch, detail=_clallam_detail(), results=page, query=_CLALLAM_SHORT)

        assert pp.resolve_mailing("clallam", [_CLALLAM_SHORT])[_CLALLAM_SHORT].outcome == AMBIGUOUS

    def test_callers_that_disagree_on_the_spelling_get_no_equivalence(self, monkeypatch):
        # Same zero-stripped key, but only one spelling is the statewide 12-digit id: the
        # other must not borrow its equivalence, so neither is answered by the land row.
        _clallam_portal(monkeypatch, detail=_clallam_detail(), query=_CLALLAM_SHORT)

        out = pp.resolve_mailing("clallam", [_CLALLAM_SHORT, _SHORT_KEY])

        assert {out[p].outcome for p in (_CLALLAM_SHORT, _SHORT_KEY)} == {AMBIGUOUS}

    def test_another_county_never_equates_a_land_row(self, monkeypatch):
        site = pp.PACS_SITES["jefferson"]
        monkeypatch.setattr(pp, "_http_get", lambda s, url: _Resp(200, _CLALLAM_FORM))
        monkeypatch.setattr(pp, "_http_post", lambda s, url, data: _Resp(200, _CLALLAM_RESULTS))

        assert pp.resolve_mailing(site.county, [_CLALLAM_SHORT])[_CLALLAM_SHORT].outcome == AMBIGUOUS

    def test_an_unavailable_form_defers_without_a_search(self, monkeypatch):
        posts = _clallam_portal(monkeypatch, form=302)       # Whatcom's error redirect, live

        out = pp.resolve_mailing("clallam", [_CLALLAM_GEO])

        assert out[_CLALLAM_GEO].outcome == SOURCE_UNAVAILABLE and posts == []

    def test_a_form_that_cannot_search_by_parcel_defers_without_a_search(self, monkeypatch):
        no_parcel_mode = _CLALLAM_FORM.replace('value="account-number"', 'value="legal"')
        posts = _clallam_portal(monkeypatch, form=no_parcel_mode)

        out = pp.resolve_mailing("clallam", [_CLALLAM_GEO])

        assert out[_CLALLAM_GEO].outcome == SOURCE_UNAVAILABLE and posts == []


# ── the recovery bookkeeping the outcomes feed ────────────────────────────────

def test_a_deferred_unsupported_id_is_not_charged_but_a_prefix_match_is(monkeypatch):
    """What county_gis does with each outcome: source_unavailable rotates uncharged;
    ambiguous is recorded as an error the sweep charges, so it ends at the ceiling."""
    from src.scrapers.enrichment import county_gis

    answers = {"R03213-049-3470": pp.MailingAnswer(SOURCE_UNAVAILABLE),
               _CLALLAM_SHORT: pp.MailingAnswer(AMBIGUOUS)}
    monkeypatch.setattr(county_gis, "_resolve_bulk_mailing", lambda key, pids: answers)
    monkeypatch.setattr(county_gis, "_batch_query_wa_statewide", lambda pids, county: {})
    stats: dict = {}

    results = county_gis.batch_enrich_parcels_gis(list(answers), "clallam", "WA", stats=stats)

    assert "R03213-049-3470" not in results                               # rotates, uncharged
    assert results[_CLALLAM_SHORT]["mailing_lookup"] == "error"             # charged attempt
    assert set(stats["county_unreached"]) == set(answers)                   # both stay deferred


@pytest.mark.parametrize(("results", "detail", "expected"), [
    (_CLALLAM_RESULTS, _CLALLAM_GEO, "found"),
    (_CLALLAM_RESULTS, _CLALLAM_SHORT, "mismatch"),
    (_CLALLAM_RESULTS.replace("1 - 1 of 1", "1 - 1 of 2"), _CLALLAM_GEO, "deferred"),
])
def test_county_gis_carries_a_clallam_short_id_through_unchanged_contracts(
        monkeypatch, results, detail, expected):
    """The whole path a Clallam lead takes: county_gis -> pacs_parcel -> the portal."""
    from src.scrapers.enrichment import county_gis

    _clallam_portal(monkeypatch, detail=_clallam_detail(detail), results=results, query=_CLALLAM_SHORT)
    monkeypatch.setattr(county_gis, "_batch_query_wa_statewide", lambda pids, county: {})
    stats: dict = {}

    row = county_gis.batch_enrich_parcels_gis([_CLALLAM_SHORT], "clallam", "WA", stats=stats).get(
        _CLALLAM_SHORT) or {}

    if expected == "found":
        assert row["mailing_address"] == "PO BOX 800, PORT ANGELES, WA 98362"
        assert row["mailing_source"] == "pacs_clallam"
        assert _CLALLAM_SHORT not in stats["county_unreached"]
    elif expected == "mismatch":                       # settled: never an address
        assert (row["mailing_lookup"], row["mailing_address"]) == ("parcel_mismatch", None)
    else:                                              # ambiguous: charged, deferred
        assert row["mailing_lookup"] == "error" and not row.get("mailing_address")
        assert _CLALLAM_SHORT in stats["county_unreached"]


# ── the owner-name search ─────────────────────────────────────────────────────

def test_the_owner_name_search_carries_its_mode(monkeypatch):
    posted: list[dict] = []

    class _Session:
        headers: dict = {}

        def get(self, url, **_k):
            return _Resp(200, _GRANT_FORM)

    def _post(session, url, data, timeout):
        posted.append(dict(data))
        return _Resp(200, "<html>None found</html>")

    monkeypatch.setattr(pacs_mod, "pinned_session", lambda: _Session())
    monkeypatch.setattr(pacs_mod, "post_search", _post)
    monkeypatch.setattr(pacs_mod, "validate_scraping_target", lambda *a, **k: None)

    outcome, _ = pacs_mod.lookup_pacs_by_name(
        "https://propertysearch.grantcountywa.gov/propertyaccess/PropertySearch.aspx?cid=10", "SMITH JOHN")

    assert posted and posted[0]["propertySearchOptions$searchType"] == "owner"
    assert outcome == pacs_mod.LOOKUP_NO_MATCH


def test_a_portal_without_an_owner_search_is_never_posted_a_name(monkeypatch):
    """Benton's form offers no owner mode: the name lookup fails, and posts nothing."""
    posted: list[dict] = []

    class _Session:
        headers: dict = {}

        def get(self, url, **_k):
            return _Resp(200, _fixture("benton_pacs_search_form.html"))

    monkeypatch.setattr(pacs_mod, "pinned_session", lambda: _Session())
    monkeypatch.setattr(pacs_mod, "post_search", lambda *a, **k: posted.append(a) or _Resp(200, ""))
    monkeypatch.setattr(pacs_mod, "validate_scraping_target", lambda *a, **k: None)

    outcome, result = pacs_mod.lookup_pacs_by_name(pp.PACS_SITES["benton"].search_url, "SMITH JOHN")

    assert (outcome, result, posted) == (pacs_mod.LOOKUP_FAILED, None, [])
