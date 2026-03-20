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
                "PERSONAL REPRESENTATIVE", "ESTATE", "WILL"],
    "pre_foreclosure": ["LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
                        "DEFAULT", "FORECLOSURE"],
    "tax_delinquent": ["TAX", "DELINQUENT", "TAX LIEN", "CERTIFICATE OF DELINQUENCY"],
    "divorce": ["DIVORCE", "DISSOLUTION"],
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
        record_type = self.record_types[0] if self.record_types else "all"
        _logger.info(
            "EagleWeb scraper — %s/%s %s — %s to %s",
            self.county, self.state, record_type, date_from, date_to,
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
        """Click the Search button."""
        try:
            search_btn = self.page.locator("button:has-text('Search'), input[value='Search']").last
            await search_btn.click()
            await self.page.wait_for_timeout(3_000)
            _logger.info("Search submitted")
        except Exception as exc:
            _logger.warning("Could not click Search: %s", str(exc)[:60])

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
        """Extract records from the current EagleWeb results page.

        EagleWeb results format:
        - Column 1 (Description): Doc type + AFN number (e.g., "Deed Of Trust 7472965")
        - Column 2 (Summary): Date + Grantor + Grantee lines
        - Pagination: numbered links + [Next/Last]
        """
        import re

        records: list[ScrapedRecord] = []
        record_type = self.record_types[0] if self.record_types else "all"

        try:
            # Check for "No documents found" or "0 items found"
            page_text = await self.page.inner_text("body")
            if "No documents found" in page_text or "0 items found" in page_text:
                _logger.info("No results found on this page")
                return []

            soup = await self.get_soup_async()

            # Find results table — look for table with "Description" and "Summary" headers
            results_table = None
            for table in soup.find_all("table"):
                headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
                # EagleWeb results have Description + Summary columns
                if "DESCRIPTION" in headers or "SUMMARY" in headers:
                    results_table = table
                    break

            if not results_table:
                # Fallback: find table with most rows that has links
                tables_with_links = []
                for table in soup.find_all("table"):
                    rows = table.find_all("tr")
                    link_count = len(table.find_all("a"))
                    if len(rows) > 3 and link_count > 2:
                        tables_with_links.append((len(rows), table))
                if tables_with_links:
                    tables_with_links.sort(reverse=True)
                    results_table = tables_with_links[0][1]

            if not results_table:
                _logger.warning("Could not find results table")
                return []

            rows = results_table.find_all("tr")
            # Doc type keywords for filtering
            type_keywords = _DOC_TYPE_MAP.get(record_type, [])

            for row in rows[1:]:  # Skip header
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                # Extract Description (col 0) and Summary (col 1)
                desc_text = cells[0].get_text(strip=True)
                summary_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""

                if not desc_text or len(desc_text) < 3:
                    continue

                # Filter by doc type if specified
                if type_keywords:
                    desc_upper = desc_text.upper()
                    if not any(kw in desc_upper for kw in type_keywords):
                        continue

                record = ScrapedRecord()

                # Parse Description: "Deed Of Trust 7472965" → doc_type + AFN
                desc_parts = desc_text.strip()
                # AFN is usually the last number in the description
                afn_match = re.search(r"\b(\d{5,})\b", desc_parts)
                if afn_match:
                    record.instrument_number = afn_match.group(1)

                # Parse Summary for date, grantor, grantee
                # Format: "03/02/2026 08:04:48 AM\nGrantor: NAME\nGrantee: NAME"
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", summary_text)
                if date_match:
                    record.date_recorded = date_match.group(1)

                grantor_match = re.search(r"Grantor:\s*(.+?)(?:Grantee:|$)", summary_text)
                if grantor_match:
                    record.party_name = grantor_match.group(1).strip().rstrip(",")

                grantee_match = re.search(r"Grantee:\s*(.+?)(?:Grantor:|$)", summary_text)
                if grantee_match:
                    grantee = grantee_match.group(1).strip().rstrip(",")
                    if grantee:
                        record.heirs = grantee

                # Look for legal description in additional cells
                for cell in cells[2:]:
                    cell_text = cell.get_text(strip=True)
                    legal_keywords = ["LOT", "BLOCK", "SEC", "TWP", "ADD", "PLAT", "SUB"]
                    if any(kw in cell_text.upper() for kw in legal_keywords):
                        record.legal_description = cell_text
                        break

                # Look for parcel ID
                parcel_match = re.search(r"\b(\d{10,})\b", summary_text)
                if parcel_match:
                    record.parcel_id = parcel_match.group(1)

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page (filtered by %s)", len(records), record_type)

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
