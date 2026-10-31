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


def _clallam_portal(monkeypatch, *, form: str | int = _CLALLAM_FORM, detail: str | None = None) -> list:
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
        assert data["propertySearchOptions$geoid"] == _CLALLAM_GEO
        return _Resp(200, _CLALLAM_RESULTS)

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
        assert pp.parse_results(_CLALLAM_CRITERIA_ERROR, "630115504300000") == (pp.UNPARSED, None)

    def test_a_geographic_id_column_is_read(self):
        assert pp.parse_results(_CLALLAM_RESULTS, "630115504300000") == (FOUND, "65460")

    def test_a_row_that_only_extends_our_id_is_ambiguous_not_not_found(self):
        # The 12-digit spelling finds the 16-digit row by the portal's prefix search.
        # Which one the lead means is unproven: charged and capped, never settled absent.
        assert pp.parse_results(_CLALLAM_RESULTS, "63011550430") == (AMBIGUOUS, None)

    def test_an_unrelated_row_is_still_not_found(self):
        # The live page's header row (<th> only) and its one-cell "Page: 1" footer are
        # not data rows, so they do not make this page unreadable.
        assert pp.parse_results(_CLALLAM_RESULTS, "730000000000000") == (pp.PARCEL_NOT_FOUND, None)

    def test_a_truncated_data_row_is_unparsed_not_not_found(self):
        page = _CLALLAM_RESULTS.replace(
            "</table>", "<tr><td><input type=checkbox></td><td>99999</td></tr></table>", 1)
        assert pp.parse_results(page, "730000000000000") == (pp.UNPARSED, None)

    def test_an_unreadable_identity_cell_is_unparsed(self):
        page = _CLALLAM_RESULTS.replace(_CLALLAM_GEO, "&nbsp;")
        assert pp.parse_results(page, "630115504300000") == (pp.UNPARSED, None)


# ── the whole lookup against the portal as it behaved ─────────────────────────

class TestResolveClallam:
    def test_the_search_carries_its_mode_and_finds_the_mailing_address(self, monkeypatch):
        posts = _clallam_portal(monkeypatch, detail=_clallam_detail())

        out = pp.resolve_mailing("clallam", [_CLALLAM_GEO])

        assert out[_CLALLAM_GEO].outcome == FOUND
        assert out[_CLALLAM_GEO].mailing_address == "PO BOX 800, PORT ANGELES, WA 98362"
        assert posts and posts[0]["propertySearchOptions$searchType"] == "account-number"

    def test_the_short_spelling_is_never_settled_as_not_found(self, monkeypatch):
        site = pp.PACS_SITES["clallam"]
        monkeypatch.setattr(pp, "_http_get", lambda s, url: _Resp(200, _CLALLAM_FORM))
        monkeypatch.setattr(pp, "_http_post", lambda s, url, data: _Resp(200, _CLALLAM_RESULTS))

        out = pp.resolve_mailing(site.county, [_CLALLAM_SHORT])

        assert out[_CLALLAM_SHORT].outcome == AMBIGUOUS

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
