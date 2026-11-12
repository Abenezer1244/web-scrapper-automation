"""Clallam files a Discontinuance of Trustee's Sale under its NTS doc type.

Fixtures are real Clallam EagleWeb pages captured 2026-11-12 (read-only):
- lookup_discontinued: a document-number search for NTS 2026-1484470. It lists the
  notice itself AND 2026-1486992, filed as "Notice of Trustee Sale" with the grantee
  cell "... DISCONTINUANCE 2026-1484470".
- lookup_active: the same search for NTS 2026-1486313, which has no discontinuance.
- probate_week_0901_p2: page 2 (100 rows) of all documents recorded 09/01-09/08/2026.
Pages are served with page.route in real Chromium, so nothing reaches the network.
"""
import gzip
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.templates.eagleweb import (
    _DOC_TYPE_EXCLUDE,
    _DOC_TYPE_MAP,
    EagleWebScraper,
    _doc_type_matches,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "eagleweb_clallam_nts_lookup"
_HOST = "erecording.clallamcountywa.gov"
_BASE = f"https://{_HOST}/recorder/web/"
_RESULTS = f"https://{_HOST}/recorder/eagleweb/docSearchResults.jsp?searchId=0"


def _html(name: str) -> str:
    return gzip.decompress((_FIXTURES / f"{name}.html.gz").read_bytes()).decode("utf-8")


_LOOKUPS = {"2026-1484470": "lookup_discontinued", "2026-1486313": "lookup_active"}


def _scraper() -> EagleWebScraper:
    return EagleWebScraper(_BASE, "clallam", "WA", ["probate", "pre_foreclosure"],
                           record_type="pre_foreclosure")


async def _route_portal(scraper: EagleWebScraper, lookups: dict[str, str], searched: list[str]):
    """Serve the search form, the POST redirect, and the results page for the posted number.

    The results page is the last posted number's page for every results URL, including
    the page-size switch the lookup makes before reading."""
    current = {}

    async def serve(route):
        url = route.request.url
        if "docSearchPOST.jsp" in url:
            number = parse_qs(route.request.post_data or "").get("DocNumID", [""])[0]
            searched.append(number)
            current["n"] = number
            # A fulfilled 302 is followed outside page.route (to the real portal), so
            # redirect client-side to keep every request inside this handler.
            await route.fulfill(status=200, content_type="text/html",
                                body=f"<script>location.replace('{_RESULTS}&n={number}')</script>")
        elif "docSearchResults.jsp" in url:
            await route.fulfill(status=200, content_type="text/html", body=lookups[current["n"]])
        elif "docSearch.jsp" in url:
            await route.fulfill(status=200, content_type="text/html", body=_html("docSearch"))
        else:
            await route.fulfill(status=404, body="")
    await scraper.page.route(f"https://{_HOST}/**", serve)


def _nts(instrument: str) -> ScrapedRecord:
    record = ScrapedRecord()
    record.doc_type = f"Notice of Trustee Sale\n{instrument}"
    record.party_name = "BORROWER"
    return record


async def test_discontinuance_row_is_not_a_lead_and_the_notice_is_kept():
    """Row filter: the discontinuance (2026-1486992) is skipped; the real notice stays."""
    scraper = _scraper()
    async with scraper:
        await scraper.page.route(f"https://{_HOST}/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=_html("lookup_discontinued")))
        await scraper.page.goto(_RESULTS)
        records = await scraper._extract_page(1)
    docs = [r.doc_type for r in records]
    assert any("2026-1484470" in d for d in docs), docs
    assert not any("2026-1486992" in d for d in docs), docs
    assert not any("DISCONTINU" in (r.party_name or "") + (r.heirs or "") for r in records)


async def test_lookup_drops_a_discontinued_notice_and_keeps_an_active_one():
    scraper = _scraper()
    searched: list[str] = []
    async with scraper:
        await _route_portal(scraper, {k: _html(v) for k, v in _LOOKUPS.items()}, searched)
        other_county = _nts("4987123")  # no year-dashed instrument: never looked up
        kept = await scraper._drop_discontinued(
            [_nts("2026-1484470"), _nts("2026-1486313"), other_county])
    assert [r.doc_type.split("\n")[1] for r in kept] == ["2026-1486313", "4987123"]
    assert searched == ["2026-1484470", "2026-1486313"]


async def test_lookup_that_does_not_return_the_notice_itself_fails_loud():
    """A results page without the notice's own row is unreadable, not 'not discontinued'."""
    scraper = _scraper()
    async with scraper:
        # 2026-1486313's page answers the search for 2026-1484470: the notice is absent.
        await _route_portal(scraper, {"2026-1484470": _html("lookup_active")}, [])
        with pytest.raises(RuntimeError, match="could not verify NTS 2026-1484470"):
            await scraper._drop_discontinued([_nts("2026-1484470")])


async def test_lookup_whose_rows_do_not_add_up_to_the_portal_total_fails_loud():
    """Codex: a discontinuance listed past what was read must not pass as 'active'."""
    scraper = _scraper()
    truncated = _html("lookup_discontinued").replace("2 items found", "7 items found")
    async with scraper:
        await _route_portal(scraper, {"2026-1484470": truncated}, [])
        with pytest.raises(RuntimeError, match="showed 2 of 7 rows"):
            await scraper._instrument_discontinued("2026-1484470")


async def test_probate_extraction_drops_tod_revocations_and_keeps_deaths_and_tod_deeds():
    scraper = EagleWebScraper(_BASE, "clallam", "WA", ["probate", "pre_foreclosure"],
                              record_type="probate")
    async with scraper:
        await scraper.page.route(f"https://{_HOST}/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=_html("probate_week_0901_p2")))
        await scraper.page.goto(_RESULTS)
        records = await scraper._extract_page(1)
    found = {r.doc_type.split("\n")[-1].strip(): r.doc_type.split("\n")[0] for r in records}
    assert "2026-1486485" not in found, found                     # Revocation of TOD
    assert found.get("2026-1486473") == "Death", found
    assert found.get("2026-1486516") == "Transfer on Death Deed", found


async def test_death_certificate_extraction_keeps_only_recorded_deaths():
    """Same real page: the Death row is the only death_certificate lead; TOD deeds and
    the TOD revocation (which hit the bare DEATH code) are not."""
    scraper = EagleWebScraper(_BASE, "clallam", "WA", ["probate", "pre_foreclosure"],
                              record_type="death_certificate")
    assert scraper.require_parcel_id is False  # name-only recorded deaths are kept
    async with scraper:
        await scraper.page.route(f"https://{_HOST}/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=_html("probate_week_0901_p2")))
        await scraper.page.goto(_RESULTS)
        records = await scraper._extract_page(1)
    labels = {r.doc_type.split("\n")[0] for r in records}
    assert labels == {"Death"}, labels
    assert "2026-1486473" in {r.doc_type.split("\n")[-1].strip() for r in records}
    assert all(r.party_name and "PUBLIC" not in r.party_name for r in records)
    # The instrument keeps its year (it used to be cut to 1486473 for the CSV).
    assert {r.enrichment_data["instrument_number"] for r in records} >= {"2026-1486473"}


@pytest.mark.parametrize(("desc", "certificate"), [
    ("Death\n2026-1486473", True),
    ("DEATH CERTIFICATE 4602631", True),
    ("Certificate of Death\n2026-1480001", True),
    ("Affidavit of Death\n2026-1480002", False),           # Codex counterexample
    ("Transfer-on-Death Deed\n2026-1480003", False),       # hyphenated TOD deed
    ("Revocation of Transfer on Death Deed\n2026-1486485", False),
])
def test_death_certificate_needs_the_whole_label(desc, certificate):
    from src.scrapers.templates.eagleweb import _DEATH_CERT_LABELS, _doc_label

    assert (_doc_label(desc) in _DEATH_CERT_LABELS) is certificate


@pytest.mark.parametrize("label", [
    "WRIT OF EXEC\n2026-1480000",                                # Clallam EXEC code
    "REVOCATION OF TRANSFER ON DEATH DEED\n2026-1486485",        # live Clallam row
])
def test_probate_keyword_hits_that_are_not_probate_are_excluded(label):
    """Both labels hit a shared probate keyword (EXEC / TRANSFER ON DEATH)."""
    assert _doc_type_matches(label, _DOC_TYPE_MAP["probate"])  # the keyword still hits ...
    assert any(neg in label for neg in _DOC_TYPE_EXCLUDE["probate"])  # ... and is excluded
