"""Cowlitz Laserfiche WebLink recorder: complete capture, full entry properties, parcels.

Live facts behind these tests (read-only, 2026-11-15):
* 10/15-11/14/2026 reported 1,428 results; the old scroll loop captured 1,068 and
  returned them as complete. Single days captured fully (97/97, 74/74).
* The results grid shows only the FIRST value of a multi-valued field. The entry
  properties of NTS 3799745 (fixture, names redacted) list five grantees and two
  parcels; the grid showed one of each.
* Cowlitz indexes the TRUSTEE as grantor on a notice of trustee's sale, sometimes an
  individual attorney, so the borrower must come from the grantees.
"""
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.scrapers.enrichment.county_gis import _KNOWN_GIS_ENDPOINTS
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.templates.laserfiche_weblink import (
    LaserficheWebLinkScraper,
    entry_contradicts_row,
    parse_entry_html,
    parse_result_count,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "laserfiche_cowlitz"
_BASE = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"


def _scraper(record_type: str) -> LaserficheWebLinkScraper:
    return LaserficheWebLinkScraper(_BASE, "cowlitz", "WA", record_type=record_type)


def _row(doc_type: str, afn: str = "3799745-0") -> dict:
    return {"afn": afn, "date": "11/12/2026", "doc_type": doc_type, "volume": "", "page_num": "",
            "grantor": "", "grantee": "", "parcel": "", "href": "/WLAUDPublic/DocView.aspx?id=1"}


def _nts_fields() -> dict[str, list[str]]:
    html = (_FIXTURES / "docview_3799745.html").read_text(encoding="utf-8")
    return parse_entry_html(BeautifulSoup(html, "lxml"))


class TestParsers:
    def test_count_reads_commas_and_zero(self):
        assert parse_result_count("Click Submit\n1,428 Results\n") == 1428
        assert parse_result_count("0 Results\nNo results found") == 0

    def test_an_unrendered_viewer_has_no_fields(self):
        assert parse_entry_html(BeautifulSoup("<p>Press Alt + R</p>", "lxml")) == {}

    def test_count_absent_is_none_not_zero(self):
        assert parse_result_count("Click Submit to search for documents.") is None

    def test_entry_fields_keep_every_value_of_a_multi_valued_field(self):
        f = _nts_fields()
        assert f["Rec Doc AFN"] == ["3799745"]
        assert f["Rec Doc Type Desc"] == ["Notice Of Trustees Sale"]
        assert f["Rec Doc Related AFN"] == ["3718972"]
        assert f["Rec Doc Document Type"] == ["NTTRUS"]
        assert len(f["Rec Doc Grantee"]) == 5
        assert f["Rec Doc Parcel"] == ["61896", "6190401"]


class TestEntryMatchesRow:
    """The grid shows each field's first value (party names cut to the column width);
    entry properties missing any of them were not fully loaded and must not be used."""

    _GRID = {"doc_type": "Notice Of Trustees Sale", "parcel": "61896",
             "grantor": "<span>QUALITY LOAN SERVICE CORPORATI</span>", "grantee": "ALPHA TIFFANY"}

    def test_complete_properties_match_the_grid_row(self):
        assert entry_contradicts_row(self._GRID, _nts_fields()) is None

    def test_a_missing_parcel_list_is_incomplete(self):
        fields = {k: v for k, v in _nts_fields().items() if k != "Rec Doc Parcel"}
        assert "parcel" in entry_contradicts_row(self._GRID, fields)

    def test_a_missing_grantee_list_is_incomplete(self):
        fields = {k: v for k, v in _nts_fields().items() if k != "Rec Doc Grantee"}
        assert "grantee" in entry_contradicts_row(self._GRID, fields)

    def test_another_document_type_is_rejected(self):
        assert "document type" in entry_contradicts_row({**self._GRID, "doc_type": "Lis Pendens"}, _nts_fields())

    def test_a_row_without_a_parcel_needs_none_in_the_properties(self):
        fields = {k: v for k, v in _nts_fields().items() if k != "Rec Doc Parcel"}
        assert entry_contradicts_row({**self._GRID, "parcel": ""}, fields) is None


class TestBuildRecord:
    def test_nts_borrower_comes_from_the_grantees_not_the_trustee(self):
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale"), _nts_fields())
        # The primary grantee is the lead; another person on the notice is not.
        assert rec.party_name == "ALPHA TIFFANY"
        # Every other indexed party stays visible, in index order: trustee, title
        # company, lender, MERS and the other person.
        assert rec.heirs == ("QUALITY LOAN SERVICE CORPORATION / FIDELITY NATIONAL TITLE COMPANY / "
                             "GUILD MORTGAGE COMPANY / MORTGAGE ELECTRONIC REGISTRATION SYSTEMS / "
                             "BRAVO CHAD D")

    def test_co_borrowers_sharing_the_surname_stay_on_the_lead(self):
        fields = {"Rec Doc AFN": ["3799690"], "Rec Doc Grantor": ["AFFINIA DEFAULT SERVICES LLC"],
                  "Rec Doc Grantee": ["CAMBA SAMPLE A", "CAMBA SAMPLE L", "SAMPLE BANK NA"],
                  "Rec Doc Parcel": ["20818"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "3799690-0"), fields)
        assert rec.party_name == "CAMBA SAMPLE A / CAMBA SAMPLE L"

    def test_an_agency_beside_the_defendant_never_joins_the_lead(self):
        fields = {"Rec Doc AFN": ["3799218"],
                  "Rec Doc Grantor": ["LISTEK SAMPLE A", "SECRETARY OF HOUSING & URBAN DEVELOPMENT"],
                  "Rec Doc Grantee": ["LAKEVIEW LOAN SERVICING LLC"], "Rec Doc Parcel": ["5064907"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Lis Pendens", "3799218-0"), fields)
        assert rec.party_name == "LISTEK SAMPLE A"
        assert "SECRETARY OF HOUSING" in rec.heirs

    def test_an_individual_attorney_trustee_is_never_the_borrower(self):
        fields = {"Rec Doc AFN": ["3799365"], "Rec Doc Grantor": ["TRUSTEE ATTORNEY M"],
                  "Rec Doc Grantee": ["SAMPLE BORROWER K"], "Rec Doc Parcel": ["21317"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "3799365-0"), fields)
        assert rec.party_name == "SAMPLE BORROWER K"
        assert rec.heirs == "TRUSTEE ATTORNEY M"

    def test_a_compound_surname_keeps_only_the_primary_person(self):
        fields = {"Rec Doc AFN": ["1"], "Rec Doc Grantor": ["SAMPLE TRUSTEE LLC"],
                  "Rec Doc Grantee": ["DE LA CRUZ ANA", "DE LEON JOSE"], "Rec Doc Parcel": ["03931"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "1-0"), fields)
        assert rec.party_name == "DE LA CRUZ ANA"
        assert "DE LEON JOSE" in rec.heirs

    def test_hyphenated_surnames_sharing_a_prefix_are_not_co_borrowers(self):
        fields = {"Rec Doc AFN": ["1"], "Rec Doc Grantor": ["SAMPLE TRUSTEE LLC"],
                  "Rec Doc Grantee": ["SMITH-JONES ANA", "SMITH-BROWN JOSE"], "Rec Doc Parcel": ["03931"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "1-0"), fields)
        assert rec.party_name == "SMITH-JONES ANA"
        assert "SMITH-BROWN JOSE" in rec.heirs

    def test_punctuation_does_not_split_co_borrowers(self):
        fields = {"Rec Doc AFN": ["1"], "Rec Doc Grantor": ["SAMPLE TRUSTEE LLC"],
                  "Rec Doc Grantee": ["SMITH, JANE", "SMITH JOHN"], "Rec Doc Parcel": ["03931"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "1-0"), fields)
        assert rec.party_name == "SMITH, JANE / SMITH JOHN"

    def test_nts_with_no_person_grantee_is_dropped(self):
        fields = {"Rec Doc AFN": ["3799644"], "Rec Doc Grantor": ["WESTERN PROGRESSIVE"],
                  "Rec Doc Grantee": ["CITIGROUP MORTGAGE LOAN TRUST"], "Rec Doc Parcel": []}
        assert _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale"), fields) is None

    def test_discontinuance_and_trustee_admin_are_not_pre_foreclosure(self):
        s = _scraper("pre_foreclosure")
        assert s._type_matches("Notice Of Trustees Sale")
        assert s._type_matches("Lis Pendens")
        for label in ("Discont Of Trustees Sale", "Substitution Of Trustee", "Deed Of Trust", "Reconveyance"):
            assert not s._type_matches(label), label

    def test_several_parcels_are_held_ambiguous_never_picked(self):
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale"), _nts_fields())
        assert rec.parcel_id is None
        assert rec.enrichment_data["recording_apn_lookup"] == "ambiguous"
        assert rec.enrichment_data["apn_candidates"] == ["61896", "6190401"]
        assert rec.enrichment_data["related_afns"] == ["3718972"]
        assert rec.enrichment_data["doc_type_code"] == "NTTRUS"

    def test_a_single_short_parcel_keeps_its_leading_zero(self):
        fields = {"Rec Doc AFN": ["3799744"], "Rec Doc Grantor": ["CLEAR RECON CORP"],
                  "Rec Doc Grantee": ["SAMPLE BORROWER C"], "Rec Doc Parcel": ["03931"]}
        rec = _scraper("pre_foreclosure")._build_record(_row("Notice Of Trustees Sale", "3799744-0"), fields)
        assert rec.parcel_id == "03931"
        assert rec.enrichment_data["recording_apn_lookup"] == "found"

    def test_probate_death_certificate_promotes_the_decedent_grantee(self):
        fields = {"Rec Doc AFN": ["3799666"], "Rec Doc Grantor": ["STATE OF WASHINGTON DEPARTMENT OF HEALTH"],
                  "Rec Doc Grantee": ["SAMPLE DECEDENT W"], "Rec Doc Parcel": ["63174069"]}
        rec = _scraper("probate")._build_record(_row("Death Certificate", "3799666-0"), fields)
        assert rec.party_name == "SAMPLE DECEDENT W"


class _FixturePortal(LaserficheWebLinkScraper):
    """One-day searches as observed live: whole, unless the day is listed short (one row
    lost to the virtual scroll)."""

    def __init__(self, per_day: dict[str, int], short_days: frozenset[str] = frozenset()):
        super().__init__(_BASE, "cowlitz", "WA", record_type="pre_foreclosure")
        self.per_day, self.short_days, self.searches = per_day, short_days, []

    async def _search(self, date_from, date_to):
        assert date_from == date_to, "searches must be one day"
        self.searches.append((date_from, date_to))
        total = self.per_day.get(date_from, 0)
        rows = [{"afn": f"{date_from}-{i}", "href": f"{date_from}/{i}"} for i in range(total)]
        if date_from in self.short_days:
            rows = rows[:-1]
        return total, rows


class TestCompleteCapture:
    @pytest.mark.asyncio
    async def test_every_day_is_searched_on_its_own_and_read_whole(self):
        portal = _FixturePortal({"10/31/2026": 3, "11/01/2026": 0, "11/02/2026": 4})
        progress = []
        portal.on_progress = lambda done, total, n, unit=None: progress.append((done, total, unit))
        rows = await portal._collect("10/31/2026", "11/02/2026")  # crosses a month boundary
        assert len(rows) == 7 and len({r["href"] for r in rows}) == 7
        assert portal.searches == [("10/31/2026", "10/31/2026"), ("11/01/2026", "11/01/2026"),
                                   ("11/02/2026", "11/02/2026")]
        assert progress[-1] == (3, 3, "day")

    @pytest.mark.asyncio
    async def test_a_day_read_short_fails_the_job(self):
        portal = _FixturePortal({"11/02/2026": 2, "11/03/2026": 4}, short_days=frozenset({"11/03/2026"}))
        with pytest.raises(ScraperExecutionError, match="captured 3 of 4 rows for 11/03/2026"):
            await portal._collect("11/02/2026", "11/03/2026")

    @pytest.mark.asyncio
    async def test_reversed_dates_fail_instead_of_reading_nothing(self):
        with pytest.raises(ScraperExecutionError, match="after"):
            await _FixturePortal({})._collect("11/08/2026", "11/07/2026")

    @pytest.mark.asyncio
    async def test_a_verified_empty_window_is_zero_rows(self):
        portal = _FixturePortal({})
        assert await portal._collect("11/07/2026", "11/08/2026") == []
        assert len(portal.searches) == 2


def test_cowlitz_county_gis_reads_five_character_parcels():
    assert _KNOWN_GIS_ENDPOINTS["cowlitz_WA"]["min_parcel_len"] == 5


# ── The real extraction methods, in Chromium, on rendered portal pages saved live
# 2026-11-15 (party names redacted, scripts removed). grid_20261112.html is the first
# 40 rows of the 11/12/2026 results; docview_3799745.html is NTS 3799745's viewer.

@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            yield await b.new_page()
        finally:
            await b.close()


def _on(page, record_type="pre_foreclosure"):
    s = _scraper(record_type)
    s.page = page
    return s


@pytest.mark.asyncio
async def test_extract_rows_reads_every_rendered_row_with_its_document_link(page):
    await page.set_content((_FIXTURES / "grid_20261112.html").read_text(encoding="utf-8"))
    rows = await _on(page)._extract_rows()
    assert len(rows) == 40
    assert all(r["href"].startswith("/WLAUDPublic/DocView.aspx?id=") for r in rows)
    assert len({r["href"] for r in rows}) == 40  # the dedupe key is unique per row
    # "3799796-1" is a real second entry under one recording number.
    assert all(r["date"] == "11/12/2026" and re.fullmatch(r"\d{7}-\d+", r["afn"]) for r in rows)
    assert "3799796-1" in {r["afn"] for r in rows}
    assert all(r["doc_type"] for r in rows)


@pytest.mark.asyncio
async def test_entry_fields_read_the_saved_viewer(page):
    await page.set_content((_FIXTURES / "docview_3799745.html").read_text(encoding="utf-8"))
    fields = await _on(page)._entry_fields("3799745-0")
    assert fields["Rec Doc Parcel"] == ["61896", "6190401"]
    assert fields["Rec Doc Grantee"][0] == "ALPHA TIFFANY" and len(fields["Rec Doc Grantee"]) == 5
    assert fields["Rec Doc Related AFN"] == ["3718972"]


@pytest.mark.asyncio
async def test_entry_fields_naming_another_document_fail(page):
    await page.set_content((_FIXTURES / "docview_3799745.html").read_text(encoding="utf-8"))
    with pytest.raises(ScraperExecutionError, match="name AFN"):
        await _on(page)._entry_fields("3799746-0")


@pytest.mark.asyncio
async def test_a_viewer_that_never_shows_properties_fails(page):
    await page.set_content("<html><body>Press Alt + R to read the document text</body></html>")
    s = _on(page)

    async def _no_wait(ms):
        return None

    page.wait_for_timeout = _no_wait  # the 20 s settle loop, run instantly
    with pytest.raises(ScraperExecutionError, match="never settled"):
        await s._entry_fields("3799745-0")



@pytest.mark.asyncio
async def test_a_death_certificate_viewer_without_a_parcel_reads_complete(page):
    await page.set_content((_FIXTURES / "docview_dc_3799666.html").read_text(encoding="utf-8"))
    fields = await _on(page, "probate")._entry_fields("3799666-0")
    assert fields["Rec Doc Grantee"] == ["SAMPLE DECEDENT W"]
    assert "Rec Doc Parcel" not in fields  # an empty field is omitted, not left loading


def test_a_half_rendered_properties_table_is_not_complete():
    from src.scrapers.templates.laserfiche_weblink import entry_render_complete

    soup = BeautifulSoup((_FIXTURES / "docview_3799745.html").read_text(encoding="utf-8"), "lxml")
    assert entry_render_complete(soup)
    labelled = [tr for tr in soup.select("#metadataTable tr.fieldPane") if tr.find_all("td", recursive=False)]
    for tr in labelled[-3:]:  # grantor, grantee, parcel not rendered yet
        tr.decompose()
    assert not entry_render_complete(soup)


@pytest.mark.asyncio
async def test_a_viewer_stuck_half_rendered_fails_instead_of_dropping_parties(page):
    soup = BeautifulSoup((_FIXTURES / "docview_3799745.html").read_text(encoding="utf-8"), "html.parser")
    labelled = [tr for tr in soup.select("#metadataTable tr.fieldPane") if tr.find_all("td", recursive=False)]
    for tr in labelled[-3:]:
        tr.decompose()
    await page.set_content(str(soup))
    s = _on(page)

    async def _no_wait(ms):
        return None

    page.wait_for_timeout = _no_wait
    with pytest.raises(ScraperExecutionError, match="never settled"):
        await s._entry_fields("3799745-0")
