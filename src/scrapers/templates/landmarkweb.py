"""LandmarkWeb template scraper for Hyland LandmarkWeb recorder portals.

Covers WA counties using the Hyland LandmarkWeb interface.
No Claude AI needed — standardized navigation + extraction.

LandmarkWeb sites share:
- Disclaimer modal with "Accept" button (SetDisclaimer() JS function)
- Record Date search at /search/index?section=searchCriteriaRecordDate
- Date picker inputs (begin/end date)
- Document type multi-select (Select2 widget)
- Results grid (#resultsGridDiv)
- Pagination via "Next" button

Counties using LandmarkWeb in WA:
King (recordsearch.kingcounty.gov)
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord, normalize_party_text
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.landmarkweb")

# Document type keywords for filtering (same as other templates)
_DOC_TYPE_MAP = {
    "probate": ["PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
                "PERSONAL REPRESENTATIVE", "PERSONAL REP", "ESTATE", "WILL",
                "DEATH", "AFFIDAVIT OF HEIRSHIP", "HEIR"],
    "pre_foreclosure": ["LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
                        "TRUSTEE'S SALE", "DISCONTINUANCE TRUSTEE",
                        "SUBSTITUTION OF TRUSTEE", "DEFAULT", "FORECLOSURE",
                        "NOTICE OF DEFAULT"],
    "tax_delinquent": ["TAX", "DELINQUENT", "TAX LIEN", "CERTIFICATE OF DELINQUENCY",
                       "CERTIFICATE OF SALE"],
    "divorce": ["DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION"],
}


class LandmarkWebScraper(BridgeScraper):
    """Template scraper for all Hyland LandmarkWeb recorder sites.

    Zero Claude AI cost — uses standardized selectors for the
    shared LandmarkWeb interface.
    """

    def __init__(self, base_url: str, county: str, state: str, record_types: list[str] | None = None):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []

        from urllib.parse import urlparse
        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from a LandmarkWeb site using date chunking."""
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7

        _logger.info(
            "LandmarkWeb scraper — %s/%s — %s to %s (%d-day chunks)",
            self.county, self.state, date_from, date_to, chunk_days,
        )

        # Navigate to the main page first
        await self.navigate(self.base_url)

        # Accept disclaimer
        await self._accept_disclaimer()

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            # Navigate to Record Date search
            await self._go_to_date_search()

            # Fill dates and submit
            await self._fill_dates(cf, ct)
            await self._submit_search()

            # Extract all pages
            chunk_records = await self._extract_all_pages()

            new_count = self.dedupe_extend(chunk_records, seen_hashes, all_records)

            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new_count, len(all_records))

            chunk_start = chunk_end
            pass

        # Enrich records with parcel data
        _logger.info("landmarkweb complete - %d records (enrichment runs after save)", len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Accept the LandmarkWeb disclaimer modal."""
        _logger.info("Page URL: %s", self.page.url)
        try:
            await self.page.wait_for_timeout(2_000)

            # LandmarkWeb uses a modal with Accept/No buttons
            # The Accept button calls SetDisclaimer() JS function
            # Try JS function first (works even when button is hidden behind modal)
            has_fn = await self.page.evaluate("typeof SetDisclaimer === 'function'")
            if has_fn:
                _logger.info("Calling SetDisclaimer() via JS")
                await self.page.evaluate("SetDisclaimer()")
                await self.page.wait_for_timeout(3_000)
                _logger.info("Disclaimer accepted via JS")
            else:
                # Fallback: click the button
                accept_btn = self.page.locator(
                    "button:has-text('Accept'), a:has-text('Accept'), "
                    "input[value*='Accept' i], "
                    "#btnDisclaimerAccept, [onclick*='SetDisclaimer']"
                )
                if await accept_btn.count() > 0:
                    _logger.info("Found disclaimer Accept button")
                    try:
                        async with self.page.expect_navigation(timeout=15_000):
                            await accept_btn.first.click()
                        _logger.info("Disclaimer accepted via navigation")
                    except Exception:
                        await accept_btn.first.click()
                        await self.page.wait_for_timeout(3_000)
                        _logger.info("Disclaimer accepted (modal close)")
                else:
                    _logger.info("No disclaimer modal found")

            _logger.info("After disclaimer URL: %s", self.page.url)
        except Exception as exc:
            _logger.info("Disclaimer error: %s", str(exc)[:100])

    async def _go_to_date_search(self) -> None:
        """Navigate to the Record Date search form."""
        try:
            # Try clicking the Record Date search icon/link
            date_link = self.page.locator(
                "#searchCriteriaRecordDate-tab, "
                "a[href*='RecordDate'], a[href*='recordDate'], "
                "a:has-text('Record Date'), "
                "[data-section='searchCriteriaRecordDate']"
            )
            if await date_link.count() > 0:
                await date_link.first.click()
                await self.page.wait_for_timeout(2_000)
                _logger.info("Navigated to Record Date search via link")
                return
        except Exception:
            pass

        # Fallback: navigate directly
        search_url = f"{self.base_url}/search/index?theme=.blue&section=searchCriteriaRecordDate"
        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15_000)
        await self.page.wait_for_timeout(3_000)
        _logger.info("Navigated to Record Date search via URL")

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the date picker inputs."""
        try:
            await self.page.wait_for_timeout(2_000)

            # Log page elements for debugging
            page_info = await self.page.evaluate("""
                (() => {
                    const info = { inputs: [], selects: [], buttons: [] };
                    document.querySelectorAll('input').forEach(el => {
                        info.inputs.push({id: el.id, name: el.name, type: el.type, placeholder: el.placeholder || ''});
                    });
                    document.querySelectorAll('select').forEach(el => {
                        info.selects.push({id: el.id, name: el.name});
                    });
                    return info;
                })()
            """)
            _logger.info("Page inputs: %d, selects: %d", len(page_info.get('inputs', [])), len(page_info.get('selects', [])))
            for inp in page_info.get('inputs', [])[:15]:
                _logger.info("  input: id=%s name=%s type=%s placeholder=%s", inp.get('id'), inp.get('name'), inp.get('type'), inp.get('placeholder'))

            # Try common LandmarkWeb date field patterns
            filled = False
            for begin_sel, end_sel in [
                # Common LandmarkWeb patterns
                ("#beginDate-RecordDate", "#endDate-RecordDate"),
                ("#beginDate", "#endDate"),
                ("input[name='beginDate']", "input[name='endDate']"),
                ("input[name*='begin' i]", "input[name*='end' i]"),
                ("#txtBeginDate", "#txtEndDate"),
                ("input[placeholder*='Begin']", "input[placeholder*='End']"),
                ("input[placeholder*='From']", "input[placeholder*='To']"),
            ]:
                begin_el = self.page.locator(begin_sel)
                end_el = self.page.locator(end_sel)
                if await begin_el.count() > 0 and await end_el.count() > 0:
                    await begin_el.first.click()
                    await begin_el.first.fill("")
                    await begin_el.first.press_sequentially(date_from, delay=30)
                    await begin_el.first.press("Tab")

                    await end_el.first.click()
                    await end_el.first.fill("")
                    await end_el.first.press_sequentially(date_to, delay=30)
                    await end_el.first.press("Tab")

                    _logger.info("Dates filled via %s / %s", begin_sel, end_sel)
                    filled = True
                    break

            if not filled:
                # Last resort: find date-type inputs
                date_inputs = await self.page.locator(
                    "input[type='date'], input[type='text'][class*='date' i], "
                    "input[data-role*='date' i]"
                ).all()
                if len(date_inputs) >= 2:
                    await date_inputs[0].click()
                    await date_inputs[0].fill("")
                    await date_inputs[0].press_sequentially(date_from, delay=30)
                    await date_inputs[1].click()
                    await date_inputs[1].fill("")
                    await date_inputs[1].press_sequentially(date_to, delay=30)
                    _logger.info("Dates filled via last-resort date inputs")
                else:
                    _logger.warning("Could not find date inputs on page")

            await self.page.wait_for_timeout(1_000)

        except Exception as exc:
            _logger.warning("Could not set dates: %s", str(exc)[:120])

    async def _submit_search(self) -> None:
        """Click the Submit button and wait for results."""
        try:
            # Try JS click first (avoids overlay/visibility issues)
            clicked = await self.page.evaluate("""() => {
                const btn = document.querySelector(
                    '#submit-RecordDate, #btnSubmit, .submitButton, ' +
                    'a.submitButton, button.submitButton'
                );
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            if clicked:
                _logger.info("Submit clicked via JS, waiting for results...")
            else:
                # Fallback to Playwright click
                submit_btn = self.page.locator(
                    "#submit-RecordDate, "
                    "a:has-text('Submit'), button:has-text('Submit'), "
                    "input[type='submit'][value*='Submit' i], "
                    "#btnSubmit, .submitButton"
                )
                if await submit_btn.count() > 0:
                    await submit_btn.first.click(force=True)
                    _logger.info("Submit clicked via Playwright, waiting for results...")
                else:
                    _logger.warning("No submit button found")
                    return

            # Wait for results grid
            try:
                await self.page.wait_for_selector(
                    "#searchResults table tbody tr, "
                    "#resultsTable tbody tr, "
                    "#resultsGridDiv table tbody tr, "
                    "#resultsGrid tbody tr, "
                    ".search-results tbody tr, "
                    ".dataTables_wrapper tbody tr, "
                    ".no-results, #noResults",
                    timeout=60_000,
                )
                _logger.info("Results selector found")
            except Exception:
                # Some LandmarkWeb sites (Clark) take 10-15s to render DataTable
                await self.page.wait_for_timeout(10_000)
                _logger.info("Results selector timeout, waiting extra for AJAX...")

            # Extra wait for AJAX / DataTable render
            await self.page.wait_for_timeout(5_000)

        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:120])

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages."""
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1

            records = await self._extract_page()
            new_count = self.dedupe_extend(records, seen_hashes, all_records)
            _logger.info("Page %d — %d new records (total: %d)", page_num, new_count, len(all_records))

            if new_count == 0:
                break

            has_next = await self._go_next_page()
            if not has_next:
                break

            pass

        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current results page.

        LandmarkWeb renders results in an HTML table within #resultsGridDiv.
        Columns typically: Recording #, Record Date, Doc Type, Grantor, Grantee, Legal
        """
        records: list[ScrapedRecord] = []

        try:
            # Log grid diagnostics
            diag = await self.page.evaluate("""
                (() => {
                    const d = {
                        results_div: !!document.querySelector('#resultsGridDiv'),
                        tables: document.querySelectorAll('#searchResults table, #resultsTable, #resultsGridDiv table, .search-results table').length,
                        rows: document.querySelectorAll('#searchResults tbody tr, #resultsTable tbody tr, #resultsGridDiv tbody tr, .search-results tbody tr').length,
                        all_rows: document.querySelectorAll('table tbody tr').length,
                    };
                    // Get column headers
                    const ths = document.querySelectorAll('#searchResults th, #resultsTable th, #resultsGridDiv th, .search-results th');
                    d.headers = Array.from(ths).map(th => th.textContent?.trim() || '');
                    return d;
                })()
            """)
            _logger.info("Grid diagnostics: %s", diag)

            # Extract rows from results table
            raw = await self.page.evaluate("""
                (() => {
                    // Find the results table — try specific selectors then fallback to largest table
                    let tables = document.querySelectorAll(
                        '#searchResults table, #resultsTable, #resultsGridDiv table, .search-results table, #resultsGrid, table.dataTable'
                    );
                    if (!tables.length) {
                        // Fallback: find the table with the most rows (likely results)
                        const allTables = document.querySelectorAll('table');
                        let best = null, bestRows = 0;
                        for (const t of allTables) {
                            const rows = t.querySelectorAll('tbody tr').length;
                            if (rows > bestRows) { bestRows = rows; best = t; }
                        }
                        if (best && bestRows >= 5) tables = [best];
                    }
                    if (!tables.length) return [];

                    const rows = tables[0].querySelectorAll('tbody tr');
                    if (!rows.length) return [];

                    // Get header mapping
                    const headers = Array.from(tables[0].querySelectorAll('th'))
                        .map(th => (th.textContent || '').trim().toLowerCase());

                    return Array.from(rows).map(row => {
                        const cells = Array.from(row.querySelectorAll('td'));
                        if (cells.length < 3) return null;

                        const data = {};
                        cells.forEach((cell, i) => {
                            const text = (cell.textContent || '').trim();
                            const header = headers[i] || '';

                            if (/record.*#|recording.*#|instrument/i.test(header)) data.instrument = text;
                            else if (/record.*date|date.*record/i.test(header)) data.date_recorded = text;
                            else if (/doc.*type|document.*type|type/i.test(header)) data.doc_type = text;
                            // Party cells: read innerHTML so the structural
                            // <div class='nameSeperator'></div> survives to Python's
                            // normalize_party_text(). textContent would collapse
                            // stacked co-owners in-browser before we can split them.
                            else if (/grantor|direct/i.test(header)) data.grantor = (cell.innerHTML || '').trim();
                            else if (/grantee|indirect/i.test(header)) data.grantee = (cell.innerHTML || '').trim();
                            else if (/legal/i.test(header)) data.legal = text;
                            else if (/parcel|apn/i.test(header)) data.parcel = text;
                            else if (!data.instrument && i === 0) data.instrument = text;
                        });

                        return data;
                    }).filter(r => r !== null);
                })()
            """)

            if not raw:
                page_text = await self.page.inner_text("body")
                if "no results" in page_text.lower() or "0 record" in page_text.lower():
                    _logger.info("No results found on this page")
                else:
                    _logger.warning("Could not extract results from grid")
                    _logger.debug("Page text (300): %s", page_text[:300].replace('\n', ' '))
                return []

            for item in raw:
                if not item:
                    continue

                record = ScrapedRecord()

                inst = (item.get("instrument") or "").strip()
                if inst:
                    record.legal_description = inst

                date_str = (item.get("date_recorded") or "").strip()
                if date_str:
                    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                    if date_match:
                        record.date_recorded = date_match.group(1)
                    else:
                        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_str)
                        if iso_match:
                            record.date_recorded = f"{iso_match.group(2)}/{iso_match.group(3)}/{iso_match.group(1)}"

                doc_type = (item.get("doc_type") or "").strip().upper()

                # Filter by record type if specified
                if self.record_types and doc_type:
                    matched = False
                    for rt in self.record_types:
                        keywords = _DOC_TYPE_MAP.get(rt, [])
                        if any(kw in doc_type for kw in keywords):
                            matched = True
                            break
                    if not matched:
                        continue

                # Party names: normalize stacked owners (nameSeperator div) -> " / "
                grantor = normalize_party_text(item.get("grantor"))
                if grantor:
                    record.party_name = grantor

                grantee = normalize_party_text(item.get("grantee"))
                if grantee:
                    record.heirs = grantee

                legal = (item.get("legal") or "").strip()
                if legal and record.legal_description:
                    record.legal_description = f"{record.legal_description} | {legal}"
                elif legal:
                    record.legal_description = legal

                parcel = (item.get("parcel") or "").strip()
                if parcel:
                    record.parcel_id = parcel

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:80])

        return records

    async def _go_next_page(self) -> bool:
        """Click the Next page button."""
        try:
            next_btn = self.page.locator(
                "a:has-text('Next'), button:has-text('Next'), "
                "a[title*='Next'], .pagination .next a, "
                "a.next-page, [aria-label='Next']"
            )
            if await next_btn.count() > 0:
                first = next_btn.first
                disabled = await first.get_attribute("disabled")
                cls = await first.get_attribute("class") or ""
                if disabled or "disabled" in cls.lower():
                    return False

                await first.click()
                await self.page.wait_for_timeout(3_000)
                return True
        except Exception:
            pass
        return False
