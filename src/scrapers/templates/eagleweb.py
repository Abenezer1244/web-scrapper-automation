"""EagleWeb template scraper for Tyler Technologies recorder portals.

Covers 16+ WA counties that use the same EagleWeb interface.
No Claude AI needed — standardized navigation + extraction.

EagleWeb sites share:
- Disclaimer page with "I Acknowledge" button
- Document search form with Start/End date, Grantor/Grantee, Parcel #
- "Search All Types" checkbox (uncheck to filter by document type)
- Standardized results table with consistent column structure
- Pagination via "Next" link

Counties using EagleWeb in WA:
Benton, Clallam, Grant, Grays Harbor, Island, Jefferson, Kitsap,
Lewis, Lincoln, Mason, Okanogan, Pacific, Spokane, Stevens, Thurston, Whitman
"""

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.eagleweb")

# Document type keywords in EagleWeb checkbox labels
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


class EagleWebScraper(BridgeScraper):
    """Template scraper for all Tyler EagleWeb recorder sites.

    Zero Claude AI cost — uses standardized selectors for the shared
    EagleWeb interface used by 16+ WA counties.
    """

    def __init__(self, base_url: str, county: str, state: str, record_types: list[str] | None = None):
        super().__init__()
        self.base_url = base_url
        self.county = county
        self.state = state
        self.record_types = record_types or []

        from urllib.parse import urlparse
        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from an EagleWeb site.

        Args:
            date_from: Start date in MM/DD/YYYY format.
            date_to: End date in MM/DD/YYYY format.

        Returns:
            List of ScrapedRecord instances.
        """
        # Extract all record types — the worker doesn't pass the specific job
        # record_type to scrape(), so we search all and filter by keywords.
        # "all" means no filter — return every record found.
        record_type = "all"
        _logger.info(
            "EagleWeb scraper — %s/%s — %s to %s",
            self.county, self.state, date_from, date_to,
        )

        await self.navigate(self.base_url)

        # Step 1: Accept disclaimer if present
        await self._accept_disclaimer()

        # Step 2: Configure search form
        await self._configure_search(record_type, date_from, date_to)

        # Step 3: Submit search
        await self._submit_search()

        # Step 4: Extract results with pagination
        all_records = await self._extract_all_pages()

        _logger.info("EagleWeb scraper complete — %d records", len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Click 'I Acknowledge' disclaimer if present."""
        try:
            # EagleWeb disclaimer buttons: "I Acknowledge", "Accept", "Agree"
            clicked = await self.page.evaluate("""
                (() => {
                    // Step 1: Try "I Acknowledge" button (most EagleWeb sites)
                    const btns = document.querySelectorAll('button, input[type="button"], input[type="submit"], a');
                    for (const b of btns) {
                        const text = (b.textContent || b.value || '').trim().toLowerCase();
                        if (text.includes('acknowledge') || text.includes('accept') || text.includes('agree')) {
                            b.click();
                            return 'acknowledge';
                        }
                    }
                    // Step 2: Try "Public Login" button (Spokane-style EagleWeb)
                    for (const b of btns) {
                        const text = (b.textContent || b.value || '').trim().toLowerCase();
                        if (text === 'public login' || text.includes('public log in')) {
                            b.click();
                            return 'public_login';
                        }
                    }
                    // Step 3: Try generic "Login" button on disclaimer page
                    for (const b of btns) {
                        const text = (b.textContent || b.value || '').trim().toLowerCase();
                        if (text === 'login' || text === 'log in') {
                            b.click();
                            return 'login';
                        }
                    }
                    return null;
                })()
            """)
            if clicked:
                await self.page.wait_for_timeout(2_000)
                _logger.info("Disclaimer step 1: %s", clicked)
                # Some EagleWeb sites need a second step (Login → Public Login)
                if clicked == 'login':
                    clicked2 = await self.page.evaluate("""
                        (() => {
                            const btns = document.querySelectorAll('button, input[type="button"], input[type="submit"]');
                            for (const b of btns) {
                                const text = (b.textContent || b.value || '').trim().toLowerCase();
                                if (text === 'public login' || text.includes('public log in')) {
                                    b.click();
                                    return true;
                                }
                            }
                            return false;
                        })()
                    """)
                    if clicked2:
                        await self.page.wait_for_timeout(2_000)
                        _logger.info("Disclaimer step 2: public login")
            else:
                _logger.info("No disclaimer found, continuing")
        except Exception:
            _logger.info("No disclaimer found, continuing")

    async def _configure_search(self, record_type: str, date_from: str, date_to: str) -> None:
        """Configure EagleWeb search form.

        Strategy: Keep "Search All Types" checked and filter by doc type
        during extraction. This is more reliable than trying to check/uncheck
        individual type checkboxes (which vary per county).
        """
        # Leave "Search All Types" checked — filter by type during extraction
        _logger.info("Searching all types, will filter '%s' during extraction", record_type)

        # Fill date range via JavaScript (most reliable for EagleWeb)
        try:
            filled = await self.page.evaluate(f"""
                (() => {{
                    // Strategy 1: Find by ID patterns (RecDateIDStart/End, StartDate, etc.)
                    const idPatterns = [
                        ['RecDateIDStart', 'RecDateIDEnd'],
                        ['StartDate', 'EndDate'],
                        ['startDate', 'endDate'],
                        ['dateFrom', 'dateTo'],
                    ];
                    for (const [startId, endId] of idPatterns) {{
                        const s = document.getElementById(startId);
                        const e = document.getElementById(endId);
                        if (s && e) {{
                            s.value = '{date_from}';
                            s.dispatchEvent(new Event('change', {{bubbles: true}}));
                            e.value = '{date_to}';
                            e.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return 'id:' + startId;
                        }}
                    }}

                    // Strategy 2: Find by nearby "Start Date"/"End Date" labels
                    const allInputs = document.querySelectorAll('input[type="text"]');
                    let startInput = null, endInput = null;
                    for (const inp of allInputs) {{
                        const cell = inp.closest('td') || inp.parentElement;
                        if (!cell) continue;
                        const text = cell.textContent.toLowerCase();
                        if (text.includes('start') && (text.includes('date') || text.includes('recording'))) {{
                            startInput = inp;
                        }} else if (text.includes('end') && (text.includes('date') || text.includes('recording'))) {{
                            endInput = inp;
                        }}
                    }}
                    if (startInput && endInput) {{
                        startInput.value = '{date_from}';
                        startInput.dispatchEvent(new Event('change', {{bubbles: true}}));
                        endInput.value = '{date_to}';
                        endInput.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return 'label';
                    }}

                    // Strategy 3: Find inputs with existing date values
                    let startSet = false;
                    for (const inp of allInputs) {{
                        if (inp.value && inp.value.includes('/') && inp.value.length >= 8) {{
                            if (!startSet) {{
                                inp.value = '{date_from}';
                                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                                startSet = true;
                            }} else {{
                                inp.value = '{date_to}';
                                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return 'value';
                            }}
                        }}
                    }}
                    return startSet ? 'partial' : null;
                }})()
            """)
            _logger.info("Date range set (%s): %s to %s", filled, date_from, date_to)
        except Exception as exc:
            _logger.warning("Could not set date range: %s", str(exc)[:60])

    async def _select_doc_types(self, record_type: str) -> None:
        """Select document type checkboxes matching the record type via JavaScript."""
        keywords = _DOC_TYPE_MAP.get(record_type, [])
        if not keywords:
            return

        # Use JavaScript to find and check matching checkboxes
        # EagleWeb checkboxes have labels as sibling text nodes
        keywords_json = str(keywords)
        selected = await self.page.evaluate(f"""
            (() => {{
                const keywords = {keywords_json};
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                let count = 0;
                for (const cb of checkboxes) {{
                    if (cb.id === 'allTypes' || cb.name === 'allTypes') continue;
                    const parent = cb.parentElement;
                    if (!parent) continue;
                    const text = parent.textContent.trim().toUpperCase();
                    for (const kw of keywords) {{
                        if (text.includes(kw)) {{
                            if (!cb.checked) {{
                                cb.checked = true;
                                cb.dispatchEvent(new Event('change', {{bubbles: true}}));
                                cb.dispatchEvent(new Event('click', {{bubbles: true}}));
                                count++;
                            }}
                            break;
                        }}
                    }}
                }}
                return count;
            }})()
        """)
        _logger.info("Selected %d doc type checkboxes for %s", selected, record_type)

    async def _submit_search(self) -> None:
        """Submit the search form via JavaScript."""
        try:
            # Use JS to find and click the Search submit button
            # EagleWeb uses <input type="submit" value="Search"> not <button>
            submitted = await self.page.evaluate("""
                (() => {
                    // Try input[type=submit] first (most EagleWeb sites)
                    const submits = document.querySelectorAll('input[type="submit"]');
                    for (const s of submits) {
                        if (s.value && s.value.trim().toLowerCase() === 'search') {
                            s.click();
                            return 'input_submit';
                        }
                    }
                    // Try button elements
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.trim().toLowerCase() === 'search') {
                            b.click();
                            return 'button';
                        }
                    }
                    // Try form submit
                    const form = document.querySelector('form');
                    if (form) { form.submit(); return 'form_submit'; }
                    return null;
                })()
            """)
            # Wait for results page — EagleWeb posts to docSearchPOST.jsp
            # which redirects to docSearchResults.jsp
            try:
                await self.page.wait_for_url("**/docSearchResults*", timeout=15_000)
            except Exception:
                # If URL-based wait fails, wait for page load
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
            await self.page.wait_for_timeout(2_000)
            _logger.info("Search submitted via %s, page: %s", submitted, self.page.url)
        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:60])

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages."""
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1

            records = await self._extract_page()
            new_count = 0
            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info("Page %d — %d new records (total: %d)", page_num, new_count, len(all_records))

            if new_count == 0:
                break

            # Check for Next page link
            has_next = await self._go_next_page()
            if not has_next:
                break

            await self.polite_delay()

        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current EagleWeb results page via JavaScript.

        Uses browser-side JS extraction instead of BeautifulSoup for reliability.
        EagleWeb results: Description (doc type + AFN) | Summary (date + Grantor + Grantee)
        """
        records: list[ScrapedRecord] = []

        try:
            # Extract directly via JavaScript — more reliable than BeautifulSoup
            raw = await self.page.evaluate("""
                (() => {
                    const tables = document.querySelectorAll('table');
                    let bestTable = null, bestRows = 0;
                    for (const t of tables) {
                        const ths = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim().toUpperCase());
                        if (ths.includes('DESCRIPTION') || ths.includes('SUMMARY')) {
                            const rows = t.querySelectorAll('tr').length;
                            if (rows > bestRows) { bestRows = rows; bestTable = t; }
                        }
                    }
                    if (!bestTable) return [];

                    const results = [];
                    const rows = bestTable.querySelectorAll('tr');
                    for (let i = 1; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length < 2) continue;
                        const desc = cells[0].textContent.trim();
                        const summary = cells[1].textContent.trim();
                        if (!desc || desc.length < 3) continue;
                        results.push({desc, summary});
                    }
                    return results;
                })()
            """)

            if not raw:
                page_text = await self.page.inner_text("body")
                if "No documents found" in page_text or "0 items found" in page_text:
                    _logger.info("No results found on this page")
                else:
                    _logger.warning("Could not find results table via JS")
                return []

            import re
            for item in raw:
                desc = item.get("desc", "")
                summary = item.get("summary", "")

                record = ScrapedRecord()

                # Parse AFN from description
                afn_match = re.search(r"\b(\d{5,})\b", desc)
                if afn_match:
                    record.instrument_number = afn_match.group(1)

                # Parse date
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", summary)
                if date_match:
                    record.date_recorded = date_match.group(1)

                # Parse Grantor
                grantor_match = re.search(r"Grantor:\s*(.+?)(?:Grantee:|$)", summary, re.DOTALL)
                if grantor_match:
                    record.party_name = grantor_match.group(1).strip().rstrip(",")

                # Parse Grantee
                grantee_match = re.search(r"Grantee:\s*(.+?)(?:Grantor:|$)", summary, re.DOTALL)
                if grantee_match:
                    grantee = grantee_match.group(1).strip().rstrip(",").rstrip(".")
                    if grantee:
                        record.heirs = grantee

                # Parcel ID
                parcel_match = re.search(r"\b(\d{10,})\b", summary)
                if parcel_match:
                    record.parcel_id = parcel_match.group(1)

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:80])

        return records

    async def _go_next_page(self) -> bool:
        """Click the Next page link if present."""
        try:
            next_link = self.page.locator("a:has-text('Next'), a:has-text('next'), a:has-text('>>')")
            if await next_link.count() > 0:
                # Check if it's clickable (not disabled)
                first = next_link.first
                cls = await first.get_attribute("class") or ""
                if "disabled" in cls.lower():
                    return False
                await first.click()
                await self.page.wait_for_timeout(2_000)
                return True
        except Exception:
            pass
        return False
