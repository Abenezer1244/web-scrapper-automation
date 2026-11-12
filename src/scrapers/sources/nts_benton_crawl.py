"""Collect Benton County recorded Notices of Trustee's Sale (and their discontinuances)
from the recorder's EagleWeb search, and read each notice's recorded image.

Uses the EagleWeb template's own navigation (date chunks, largest page size, Next
links) but its own row read: every row of the search, not the lead filter, because
a Discontinuance is not a lead but cancels one. Each chunk is reconciled against
the portal's "N items found" so a short read fails instead of passing as complete.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.scrapers.sources.nts_benton_recorder import parse_recorded_nts
from src.scrapers.sources.nts_pdf import extract_pdf_text
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.nts_benton_crawl")

BASE_URL = "https://erecording.co.benton.wa.us/recorder/web/"
_NTS_ROW = re.compile(r"Notice\s+Of\s+Trustee'?s?\s+Sale|Notice\s+of\s+Trustee\s+Sale", re.I)
_DISCONTINUANCE_ROW = re.compile(r"^\s*Discontinuance\b", re.I)
_RELATED = re.compile(r"Related:\s*([\d\-,\s]+)")
_INSTRUMENT = re.compile(r"\d{4}-\d{6}")

_ROWS_JS = r"""(() => {
    const own = t => Array.from(t.querySelectorAll(':scope > thead > tr > th, :scope > tbody > tr > th'))
        .map(h => h.textContent.trim().toUpperCase());
    const table = Array.from(document.querySelectorAll('table'))
        .find(t => { const h = own(t); return h.includes('DESCRIPTION') || h.includes('SUMMARY'); });
    if (!table) return null;
    return Array.from(table.querySelectorAll(':scope > tbody > tr'))
        .filter(r => r.querySelectorAll(':scope > td').length >= 2)
        .map(r => {
            const td = r.querySelectorAll(':scope > td');
            const att = r.querySelector('a[href*="viewAttachment"]');
            const doc = r.querySelector('a[href*="viewDoc"]');
            return {desc: td[0].textContent.trim().replace(/\s+/g, ' '),
                    summary: td[1].textContent.trim().replace(/\s+/g, ' '),
                    attach: att ? att.href : '', doc: doc ? doc.href : ''};
        });
})()"""


@dataclass
class Collected:
    notices: list[tuple[dict, str]] = field(default_factory=list)   # (parsed, source_url)
    cancelled: set[str] = field(default_factory=set)                # instrument numbers
    failed: int = 0
    nts_rows: int = 0


async def collect(date_from: str, date_to: str) -> Collected:
    """Every NTS recorded in the window (parsed off its image) and every instrument a
    recorded Discontinuance in the window names."""
    from src.scrapers.templates.eagleweb import EagleWebScraper, _date_chunks

    out = Collected()
    seen: set[str] = set()  # chunk windows share their boundary day: read each document once
    scraper = EagleWebScraper(BASE_URL, "benton", "wa", ["pre_foreclosure"], record_type="pre_foreclosure")
    async with scraper:
        await scraper.navigate(BASE_URL)
        await scraper._accept_disclaimer()
        for i, (cf, ct) in enumerate(_date_chunks(date_from, date_to, 7)):
            if i:
                await scraper.page.goto(BASE_URL.replace("/web/", "/eagleweb/docSearch.jsp"),
                                        wait_until="domcontentloaded")
                await scraper._accept_disclaimer()
                await scraper.page.wait_for_selector("#RecDateIDStart", timeout=15_000)
            await scraper._configure_search("pre_foreclosure", cf, ct)
            await scraper._submit_search()
            await scraper._use_largest_page_size()
            rows: list[dict] = []
            body = await scraper.page.inner_text("body")
            m = re.search(r"([\d,]+)\s+items?\s+found", body, re.I)
            if m is None and "No documents found" not in body:
                # Neither a total nor the portal's empty marker: an error or changed
                # page, never an empty week (Codex P2).
                raise RuntimeError(f"benton NTS crawl: no result count for {cf}-{ct}")
            expected = int(m.group(1).replace(",", "")) if m else 0
            while True:
                page_rows = await scraper.page.evaluate(_ROWS_JS)
                if page_rows is None:
                    if not rows and expected == 0:
                        break
                    raise RuntimeError(f"benton NTS crawl: results page unreadable for {cf}-{ct}")
                rows += page_rows
                if len(rows) >= expected or not await scraper._go_next_page():
                    break
            if len(rows) != expected:
                raise RuntimeError(f"benton NTS crawl: read {len(rows)} rows of {expected} for {cf}-{ct}")
            for r in rows:
                if _DISCONTINUANCE_ROW.match(r["desc"]):
                    rel = _RELATED.search(r["summary"])
                    out.cancelled.update(_INSTRUMENT.findall(rel.group(1)) if rel else [])
                elif _NTS_ROW.search(r["desc"]):
                    key = r["attach"] or r["desc"]
                    if key in seen:
                        continue
                    seen.add(key)
                    out.nts_rows += 1
                    # A notice row without its image link is a failed read, never "none".
                    parsed = await _read_notice(scraper, r["attach"]) if r["attach"] else None
                    if parsed is None:
                        out.failed += 1
                    else:
                        out.notices.append((parsed, r["doc"] or BASE_URL))
            await scraper.polite_delay()
    _logger.info("benton NTS crawl %s-%s: %d NTS rows, %d parsed, %d failed, %d cancelled instruments",
                 date_from, date_to, out.nts_rows, len(out.notices), out.failed, len(out.cancelled))
    return out


async def _read_notice(scraper, attach_href: str) -> dict | None:
    """Parse one recorded notice image, or None when it cannot be read."""
    from urllib.parse import parse_qs, urlparse

    href = urlparse(attach_href)
    q = parse_qs(href.query)
    name, doc_id, parent = (q.get(k, [""])[0] for k in ("docName", "id", "parent"))
    if href.hostname != urlparse(BASE_URL).hostname or not re.fullmatch(r"\d{4}_\d{6}", name):
        return None
    await scraper.polite_delay()
    try:
        data = await scraper._fetch_document(
            f"https://{href.hostname}/recorder/eagleweb/downloads/{name}.pdf?id={doc_id}&parent={parent}")
        from src.scrapers.templates.eagleweb import _RECORDED_DOCUMENT_MAX_PAGES, _pdf_page_count

        if _pdf_page_count(data) > _RECORDED_DOCUMENT_MAX_PAGES:
            return None  # unread pages could name another property
        text = extract_pdf_text(data, max_pages=_RECORDED_DOCUMENT_MAX_PAGES)
        if len(text.strip()) < 200:
            return None
        return parse_recorded_nts(text)
    except Exception as exc:  # counted by the caller; the crawl reports failures
        _logger.warning("benton NTS crawl: %s unreadable (%s)", name, type(exc).__name__)
        return None
