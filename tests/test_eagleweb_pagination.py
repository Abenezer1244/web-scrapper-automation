"""EagleWeb paging and date-window completeness, replayed in real Chromium.

Fixture: one real Benton County results week (10/23/2026-10/30/2026, all document
types), captured 2026-11-12: 8 pages, "763 items found", 100 rows a page. Matching
pre-foreclosure rows sit on pages 1, 2, 4, 6 and 7. The old loop stopped at the
first page with no matching row (page 3), so it returned 2 of the 5. Pages are
served for the real portal host with page.route, so nothing reaches the network.
"""
import gzip
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.scrapers.templates.eagleweb import (
    EagleWebScraper,
    _date_chunks,
    _looks_like_instrument_year,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "eagleweb_benton_week"
_HOST = "erecording.co.benton.wa.us"
_RESULTS = f"https://{_HOST}/recorder/eagleweb/docSearchResults.jsp?searchId=0"
_NO_TABLE = "<html><body><h1>Service temporarily unavailable</h1></body></html>"


def _page(n: int) -> str:
    return gzip.decompress((_FIXTURES / f"page{n}.html.gz").read_bytes()).decode("utf-8")


async def _run(serve_page, max_pages: int | None = None, record_type: str = "pre_foreclosure",
               fill: bool = False, documents: Path | None = None) -> list:
    """Collect pre-foreclosure rows across the week; serve_page(n) -> HTML for page n.
    fill=True then reads each parcel-less row's recorded document (served from
    fixtures/eagleweb_benton_week/documents, the real recorder PDFs)."""
    async def serve(route):
        query = parse_qs(urlparse(route.request.url).query)
        doc = re.search(r"/downloads/(\d{4}_\d{6})\.pdf", route.request.url)
        if doc:
            path = (documents or _FIXTURES / "documents") / f"{doc.group(1)}.pdf"
            if path.exists():
                await route.fulfill(status=200, content_type="application/pdf", body=path.read_bytes())
            else:
                await route.fulfill(status=404, body="")
            return
        if "docSearchResults.jsp" not in route.request.url:
            await route.fulfill(status=404, body="")
            return
        n = int(query.get("page", ["1"])[0])
        await route.fulfill(status=200, content_type="text/html", body=serve_page(n))

    scraper = EagleWebScraper(f"https://{_HOST}/recorder/web/", "benton", "wa",
                              ["probate", "pre_foreclosure", "death_certificate"], record_type=record_type)
    if max_pages:
        scraper._MAX_PAGES = max_pages
    async with scraper:
        await scraper.page.route(f"https://{_HOST}/**", serve)
        await scraper.page.goto(_RESULTS)
        records = await scraper._extract_all_pages()
        if fill:
            await scraper._fill_parcels_from_recorded_documents(records)
        return records


def _without_total(html: str) -> str:
    """The same page on a portal that prints no "N items found" total."""
    return html.replace("763 items found", "")


async def test_reads_every_page_past_a_page_with_no_matching_rows():
    records = await _run(_page)
    numbers = sorted(r.enrichment_data["instrument_number"] for r in records)
    assert len(records) == 5, numbers  # was 2: paging stopped at page 3
    assert all("Trustee" in r.doc_type or "Lis Pendens" in r.doc_type for r in records)


async def test_a_page_that_repeats_the_previous_one_fails_loud():
    with pytest.raises(RuntimeError, match="pagination stuck"):
        await _run(lambda n: _page(3 if n == 4 else n))


async def test_a_page_that_repeats_an_earlier_one_fails_loud():
    """A, B, A, B... would still add up to the portal's total (Codex review)."""
    with pytest.raises(RuntimeError, match="pagination stuck"):
        await _run(lambda n: _page(1 if n % 2 else 2))


async def test_a_page_that_never_renders_mid_result_fails_loud_instead_of_ending_early():
    with pytest.raises(RuntimeError, match="page 5 could not be read"):
        await _run(lambda n: _NO_TABLE if n == 5 else _page(n))


async def test_rows_the_extractor_skips_still_count_as_read():
    """Island, week of 11/01/2026: 241 rows, 5 of them marriage affidavits whose summary
    has no MM/DD/YYYY date. They are not leads, but they were on the page, so the
    portal's total must still reconcile."""
    island = Path(__file__).parent / "fixtures" / "eagleweb_island_week"
    host = "auditor.islandcountywa.gov"

    async def serve(route):
        n = int(parse_qs(urlparse(route.request.url).query).get("page", ["1"])[0])
        body = gzip.decompress((island / f"page{n}.html.gz").read_bytes()).decode("utf-8")
        await route.fulfill(status=200, content_type="text/html", body=body)

    scraper = EagleWebScraper(f"https://{host}/recorder/web/", "island", "wa",
                              ["probate", "pre_foreclosure"], record_type="pre_foreclosure")
    async with scraper:
        await scraper.page.route(f"https://{host}/**", serve)
        await scraper.page.goto(f"https://{host}/recorder/eagleweb/docSearchResults.jsp?searchId=0")
        await scraper._extract_all_pages()  # raised "read 236 rows ... reported 241" before


async def test_switches_to_the_largest_results_per_page_the_portal_offers():
    """Clallam defaults to 5 rows a page (215 rows = 43 page loads a week)."""
    page1 = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "eagleweb_clallam_page" / "page1.html.gz").read_bytes()
    ).decode("utf-8")
    host = "erecording.clallamcountywa.gov"
    requested: list[str] = []

    async def serve(route):
        requested.append(route.request.url)
        await route.fulfill(status=200, content_type="text/html", body=page1)

    scraper = EagleWebScraper(f"https://{host}/recorder/web/", "clallam", "wa", ["pre_foreclosure"],
                              record_type="pre_foreclosure")
    async with scraper:
        await scraper.page.route(f"https://{host}/**", serve)
        await scraper.page.goto(f"https://{host}/recorder/eagleweb/docSearchResults.jsp?searchId=0")
        await scraper._use_largest_page_size()
        searches = [u for u in requested if "docSearchResults.jsp" in u]
        # Clallam's results table sits inside a layout table that also "has" the
        # Description header; rows must be counted on the inner one (5, not 1).
        await scraper._extract_page(1)
        assert scraper._page_rows_seen == 5
    query = parse_qs(urlparse(searches[-1]).query)
    assert query["pageSize"] == ["100"] and query["page"] == ["1"] and query["searchId"] == ["0"]


async def test_hitting_the_page_cap_with_more_pages_fails_loud():
    with pytest.raises(RuntimeError, match="3-page cap with more results"):
        await _run(lambda n: _without_total(_page(n)), max_pages=3)


async def test_a_result_that_ends_exactly_on_the_cap_page_is_complete():
    """No total to reconcile and the last page is the cap page: no Next link, no error."""
    records = await _run(lambda n: _without_total(_page(n)), max_pages=8)
    assert len(records) == 5


async def test_death_certificate_keeps_only_recorded_death_certificates():
    """Benton records Washington death certificates with the State as co-grantor; the
    week has two, on pages 2 and 5. The decedent must be the lead, not the agency."""
    records = await _run(_page, record_type="death_certificate")
    assert [r.doc_type.splitlines()[0] for r in records] == ["Cert Of Death", "Cert Of Death"]
    first = next(r for r in records if r.enrichment_data["instrument_number"] == "027085")
    assert first.party_name == "LEISTRITZ, MIKE E"
    assert first.parcel_id == "115802011127002"
    assert all("DEPT OF HEALTH" not in (r.party_name or "") for r in records)


async def test_truncated_parcels_are_read_off_the_recorded_document():
    """All 5 of the week's filings show a truncated parcel ("Parcel: 1118911...").
    Their recorded images give the full parcel; 2026-027018 also names Walla Walla
    County parcels, so it is quarantined rather than pinned to one parcel."""
    records = await _run(_page, fill=True)
    by_number = {r.enrichment_data["instrument_number"]: r for r in records}
    assert by_number["027155"].parcel_id == "111891100002006"
    assert by_number["027262"].parcel_id == "101891020008011"
    assert by_number["027553"].parcel_id == "102842040000021"
    assert by_number["027478"].parcel_id == "102984020805002"  # a lis pendens
    assert by_number["027155"].property_address == "1712S TACOMA ST, KENNEWICK, WA 99337"
    assert by_number["027155"].enrichment_data["parcel_source"] == "recorded_document"
    multi = by_number["027018"]
    assert multi.parcel_id is None and multi.enrichment_data["recorded_document_multi_property"] is True


async def test_unreadable_documents_fail_the_run_rather_than_drop_every_lead(tmp_path):
    # An empty document folder: every recorded-document read returns 404.
    with pytest.raises(RuntimeError, match="no recorded document could be read"):
        await _run(_page, fill=True, documents=tmp_path)


def test_a_same_day_window_still_searches_once():
    assert _date_chunks("11/12/2026", "11/12/2026", 7) == [("11/12/2026", "11/12/2026")]


def test_multi_day_windows_chunk_as_before():
    assert _date_chunks("10/01/2026", "10/20/2026", 7) == [
        ("10/01/2026", "10/08/2026"), ("10/08/2026", "10/15/2026"), ("10/15/2026", "10/20/2026"),
    ]
    assert _date_chunks("10/01/2026", "10/08/2026", 7) == [("10/01/2026", "10/08/2026")]


def test_an_inverted_window_is_refused():
    with pytest.raises(ValueError, match="inverted"):
        _date_chunks("10/20/2026", "10/01/2026", 7)


def test_instrument_year_filter_rolls_with_the_calendar():
    from datetime import date
    year = date.today().year
    assert _looks_like_instrument_year(f"{year}023467")
    assert _looks_like_instrument_year(f"{year + 1}000001")  # January filings of next year
    assert not _looks_like_instrument_year("1188030500020")  # a Benton parcel
    # The floor never rises: in 2027 a 2024 instrument number is still not a parcel.
    assert _looks_like_instrument_year("2024023467", year=2027)
    assert _looks_like_instrument_year("2028000001", year=2027)


def _copy_documents(dest: Path, **replace: bytes) -> Path:
    """The week's real recorded PDFs, with some replaced by the given bytes."""
    for f in (_FIXTURES / "documents").glob("*.pdf"):
        (dest / f.name).write_bytes(replace.get(f.stem, f.read_bytes()))
    return dest


def _pdf(pages_from: Path | None = None, repeat: int = 1, blank_pages: int = 0) -> bytes:
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for _ in range(repeat):
        if pages_from is not None:
            for page in PdfReader(pages_from).pages:
                w.add_page(page)
    for _ in range(blank_pages):
        w.add_blank_page(width=612, height=792)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


async def test_a_document_past_the_page_cap_is_quarantined_not_pinned(tmp_path):
    """Pages that are not read could name another property (Codex)."""
    long_doc = _pdf(_FIXTURES / "documents" / "2026_027155.pdf", repeat=5)  # 6 pages x 5 = 30
    records = await _run(_page, fill=True, documents=_copy_documents(tmp_path, **{"2026_027155": long_doc}))
    by_number = {r.enrichment_data["instrument_number"]: r for r in records}
    assert by_number["027155"].parcel_id is None
    assert by_number["027262"].parcel_id == "101891020008011"  # the others still fill


async def test_image_only_scans_count_as_failed_reads(tmp_path):
    blank = _pdf(blank_pages=4)
    docs = {f.stem: blank for f in (_FIXTURES / "documents").glob("*.pdf")}
    with pytest.raises(RuntimeError, match="no recorded document could be read"):
        await _run(_page, fill=True, documents=_copy_documents(tmp_path, **docs))


async def test_an_oversized_document_is_refused_while_streaming(tmp_path, monkeypatch):
    import src.scrapers.templates.eagleweb as ew

    monkeypatch.setattr(ew, "_RECORDED_DOCUMENT_MAX_BYTES", 50_000)  # every real NTS is larger
    with pytest.raises(RuntimeError, match="no recorded document could be read"):
        await _run(_page, fill=True)


async def test_the_document_link_never_reaches_the_record_or_its_hash():
    records = await _run(_page)
    assert all(getattr(r, "_document_href", None) for r in records)
    assert all("_document_href" not in str(r.to_dict()) and "viewAttachment" not in str(r.to_dict())
               for r in records)


async def test_a_stalled_document_download_times_out(monkeypatch):
    """A response that never arrives must not hold the run until the worker deadline."""
    import asyncio

    import src.scrapers.templates.eagleweb as ew

    monkeypatch.setattr(ew, "_RECORDED_DOCUMENT_TIMEOUT_MS", 1_500)
    scraper = EagleWebScraper(f"https://{_HOST}/recorder/web/", "benton", "wa", ["pre_foreclosure"],
                              record_type="pre_foreclosure")

    async def serve(route):
        if "/downloads/" in route.request.url:
            await asyncio.sleep(30)  # stalls far past the deadline
            return
        await route.fulfill(status=200, content_type="text/html", body=_page(1))

    async with scraper:
        await scraper.page.route(f"https://{_HOST}/**", serve)
        await scraper.page.goto(_RESULTS)
        with pytest.raises(Exception, match="(?i)abort"):
            await asyncio.wait_for(scraper._fetch_document(
                f"https://{_HOST}/recorder/eagleweb/downloads/2026_027155.pdf?id=DOC1.A0&parent=DOC1"), 10)
