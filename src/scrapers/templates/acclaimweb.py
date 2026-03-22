"""AcclaimWeb template scraper for Tyler Technologies AcclaimWeb recorder portals.

Covers WA counties using the AcclaimWeb/Harris Recording Solutions interface.
No Claude AI needed — standardized Kendo UI navigation + extraction.

AcclaimWeb sites share:
- Disclaimer page with "Accept" link (public access, no login)
- Record Date search at /search/SearchTypeRecordDate
- Kendo DatePicker widgets (#FromDatePicker, #ToDatePicker)
- Kendo Grid results (#SearchResultGrid)
- Kendo pager for pagination

Counties using AcclaimWeb in WA:
Chelan, Douglas, Pend Oreille
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.acclaimweb")

# Same doc type keywords as EagleWeb — AcclaimWeb uses similar naming
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


class AcclaimWebScraper(BridgeScraper):
    """Template scraper for all Tyler AcclaimWeb recorder sites.

    Zero Claude AI cost — uses standardized Kendo UI selectors for the
    shared AcclaimWeb interface.
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
        """Scrape records from an AcclaimWeb site using date chunking.

        AcclaimWeb can return large result sets — split into 7-day chunks
        to keep things manageable and avoid timeouts.
        """
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7

        _logger.info(
            "AcclaimWeb scraper — %s/%s — %s to %s (%d-day chunks)",
            self.county, self.state, date_from, date_to, chunk_days,
        )

        # Navigate to Record Date search page
        search_url = f"{self.base_url}/search/SearchTypeRecordDate"
        await self.navigate(search_url)

        # Accept disclaimer if shown
        await self._accept_disclaimer()

        # Ensure we're on the search page
        if "SearchTypeRecordDate" not in self.page.url:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15_000)
            await self.page.wait_for_timeout(2_000)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            # Navigate back to search for subsequent chunks
            if chunk_start != start:
                await self._go_to_search()

            # Fill dates and submit
            await self._fill_dates(cf, ct)
            await self._submit_search()

            # Extract all pages
            chunk_records = await self._extract_all_pages()

            # Deduplicate
            new_count = 0
            for record in chunk_records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new_count, len(all_records))

            if len(all_records) >= 5000:
                _logger.info("Reached 5000 record cap, stopping")
                break

            chunk_start = chunk_end
            await self.polite_delay()

        # Enrich records with parcel data
        enrichable = [r for r in all_records if r.parcel_id and len(r.parcel_id) >= 8]
        if enrichable:
            _logger.info("Enriching %d records with parcel data", len(enrichable))
            from src.scrapers.enrichment import enrich_parcel

            for record in enrichable[:200]:
                try:
                    enriched = await enrich_parcel(record.parcel_id, self.county, self.state)
                    record.property_address = enriched.get("property_address") or record.property_address
                    record.mailing_address = enriched.get("mailing_address") or record.mailing_address
                    if enriched.get("property_address"):
                        record.enrichment_data = enriched
                except Exception:
                    pass
                await self.polite_delay()

        _logger.info("AcclaimWeb scraper complete — %d records (%d enriched)", len(all_records), len(enrichable))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Click disclaimer accept link/button if present."""
        try:
            # AcclaimWeb disclaimer: look for accept/continue links
            clicked = await self.page.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a, button, input[type="button"], input[type="submit"]');
                    for (const el of links) {
                        const text = (el.textContent || el.value || '').trim().toLowerCase();
                        if (text.includes('accept') || text.includes('agree') ||
                            text.includes('acknowledge') || text.includes('continue')) {
                            el.click();
                            return text;
                        }
                    }
                    return null;
                })()
            """)
            if clicked:
                await self.page.wait_for_timeout(3_000)
                _logger.info("Disclaimer accepted: %s", clicked)
            else:
                _logger.info("No disclaimer found, continuing")
        except Exception:
            _logger.info("No disclaimer found, continuing")

    async def _go_to_search(self) -> None:
        """Navigate back to the Record Date search form."""
        search_url = f"{self.base_url}/search/SearchTypeRecordDate"
        try:
            # Try clicking search tab link first (stays in session)
            search_link = self.page.locator(
                "a[href*='SearchTypeRecordDate'], a:has-text('Record Date')"
            )
            if await search_link.count() > 0:
                await search_link.first.click()
                await self.page.wait_for_timeout(2_000)
                return
        except Exception:
            pass

        # Fallback: navigate directly
        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15_000)
        await self.page.wait_for_timeout(2_000)

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the Kendo DatePicker date inputs.

        AcclaimWeb uses Kendo UI DatePicker widgets. We use JavaScript
        to set values directly through the Kendo API, which is more
        reliable than trying to interact with the rendered input elements.
        """
        try:
            # Wait for Kendo widgets to initialize
            await self.page.wait_for_timeout(2_000)

            # First try: set "Specific Date Range" in the dropdown if it exists
            try:
                await self.page.evaluate("""
                    (() => {
                        const dd = document.querySelector('#DateRangeDropDown');
                        if (dd) {
                            const widget = $(dd).data('kendoDropDownList');
                            if (widget) {
                                widget.value('SpecificDateRange');
                                widget.trigger('change');
                            }
                        }
                    })()
                """)
                await self.page.wait_for_timeout(1_000)
            except Exception:
                pass

            # Set dates via Kendo API (most reliable for Kendo DatePicker)
            filled = await self.page.evaluate(f"""
                (() => {{
                    let filled = false;

                    // Try Kendo DatePicker API
                    if (typeof $ !== 'undefined') {{
                        const fromPicker = $('#FromDatePicker').data('kendoDatePicker');
                        const toPicker = $('#ToDatePicker').data('kendoDatePicker');
                        if (fromPicker && toPicker) {{
                            fromPicker.value('{date_from}');
                            fromPicker.trigger('change');
                            toPicker.value('{date_to}');
                            toPicker.trigger('change');
                            filled = true;
                        }}

                        // Try alternate: RecordDatePicker with date range
                        if (!filled) {{
                            const singlePicker = $('#RecordDatePicker').data('kendoDatePicker');
                            if (singlePicker) {{
                                singlePicker.value('{date_from}');
                                singlePicker.trigger('change');
                                filled = true;
                            }}
                        }}
                    }}

                    return filled;
                }})()
            """)

            if filled:
                _logger.info("Dates set via Kendo API: %s to %s", date_from, date_to)
                return

            # Fallback: type into the input elements directly
            for from_id, to_id in [
                ("FromDatePicker", "ToDatePicker"),
                ("RecordDateFrom", "RecordDateTo"),
            ]:
                from_el = self.page.locator(f"#{from_id}")
                to_el = self.page.locator(f"#{to_id}")
                if await from_el.count() > 0 and await to_el.count() > 0:
                    await from_el.click()
                    await from_el.fill("")
                    await from_el.press_sequentially(date_from, delay=30)
                    await to_el.click()
                    await to_el.fill("")
                    await to_el.press_sequentially(date_to, delay=30)
                    _logger.info("Dates typed: %s to %s", date_from, date_to)
                    return

            _logger.warning("Could not find date inputs")

        except Exception as exc:
            _logger.warning("Could not set dates: %s", str(exc)[:80])

    async def _submit_search(self) -> None:
        """Click the Search button and wait for results grid to populate."""
        try:
            # Click the search button
            search_btn = self.page.locator("#SearchBtn")
            if await search_btn.count() == 0:
                search_btn = self.page.locator(
                    "button:has-text('Search'), input[type='submit'][value='Search']"
                )

            await search_btn.first.click()

            # Wait for results grid to populate (Kendo Grid loads via AJAX)
            try:
                await self.page.wait_for_selector(
                    "#SearchResultGrid tbody tr, .k-grid-content tr, .no-results, .k-grid-norecords",
                    timeout=60_000,
                )
            except Exception:
                # May have loaded differently — check after a delay
                await self.page.wait_for_timeout(5_000)

            await self.page.wait_for_timeout(2_000)
            _logger.info("Search submitted, page: %s", self.page.url)

        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:80])

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages in the Kendo Grid."""
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

            has_next = await self._go_next_page()
            if not has_next:
                break

            await self.polite_delay()

        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current Kendo Grid results page.

        AcclaimWeb renders results in a Kendo Grid with standard columns:
        Instrument #, Record Date, Doc Type, Grantor, Grantee, Legal Description
        """
        records: list[ScrapedRecord] = []

        try:
            raw = await self.page.evaluate("""
                (() => {
                    // Try Kendo Grid data first (most reliable)
                    if (typeof $ !== 'undefined') {
                        const grid = $('#SearchResultGrid').data('kendoGrid');
                        if (grid) {
                            const data = grid.dataSource.view();
                            if (data && data.length > 0) {
                                return Array.from(data).map(item => ({
                                    instrument: item.InstrumentNumber || item.Instrument || '',
                                    date_recorded: item.RecordDate || item.RecordingDate || '',
                                    doc_type: item.DocType || item.DocumentType || item.DocTypeName || '',
                                    grantor: item.Grantor || item.GrantorName || '',
                                    grantee: item.Grantee || item.GranteeName || '',
                                    legal: item.LegalDescription || item.Legal || '',
                                    parcel: item.ParcelId || item.Parcel || item.APN || '',
                                }));
                            }
                        }
                    }

                    // Fallback: extract from DOM table rows
                    const rows = document.querySelectorAll(
                        '#SearchResultGrid tbody tr, .k-grid-content tbody tr'
                    );
                    if (!rows.length) return [];

                    return Array.from(rows).map(row => {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 4) return null;
                        return {
                            instrument: (cells[0] || {}).textContent?.trim() || '',
                            date_recorded: (cells[1] || {}).textContent?.trim() || '',
                            doc_type: (cells[2] || {}).textContent?.trim() || '',
                            grantor: (cells[3] || {}).textContent?.trim() || '',
                            grantee: (cells[4] || {}).textContent?.trim() || '',
                            legal: (cells[5] || {}).textContent?.trim() || '',
                            parcel: '',
                        };
                    }).filter(r => r !== null);
                })()
            """)

            if not raw:
                page_text = await self.page.inner_text("body")
                if "no results" in page_text.lower() or "0 records" in page_text.lower():
                    _logger.info("No results found on this page")
                else:
                    _logger.warning("Could not extract results from Kendo Grid")
                return []

            for item in raw:
                if not item:
                    continue

                record = ScrapedRecord()

                # Instrument number
                inst = item.get("instrument", "").strip()
                if inst:
                    record.legal_description = inst  # store instrument # in legal_description

                # Date recorded — normalize various date formats
                date_str = item.get("date_recorded", "").strip()
                if date_str:
                    # Handle ISO format from Kendo data source
                    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                    if date_match:
                        record.date_recorded = date_match.group(1)
                    else:
                        # Try ISO format (2025-01-15T00:00:00)
                        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_str)
                        if iso_match:
                            record.date_recorded = f"{iso_match.group(2)}/{iso_match.group(3)}/{iso_match.group(1)}"

                # Doc type — use for filtering
                doc_type = item.get("doc_type", "").strip().upper()

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

                # Grantor → party_name
                grantor = item.get("grantor", "").strip()
                if grantor:
                    record.party_name = grantor

                # Grantee → heirs
                grantee = item.get("grantee", "").strip()
                if grantee:
                    record.heirs = grantee

                # Legal description
                legal = item.get("legal", "").strip()
                if legal and record.legal_description:
                    record.legal_description = f"{record.legal_description} | {legal}"
                elif legal:
                    record.legal_description = legal

                # Parcel ID
                parcel = item.get("parcel", "").strip()
                if parcel:
                    record.parcel_id = parcel

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:80])

        return records

    async def _go_next_page(self) -> bool:
        """Click the Next page button in the Kendo pager."""
        try:
            # Kendo pager next button
            next_btn = self.page.locator(
                ".k-pager-nav[title='Go to the next page'], "
                "a[title='Next page'], "
                "button[title='Next page'], "
                ".k-i-arrow-e, "
                "a.k-link:has-text('>')"
            )
            if await next_btn.count() > 0:
                first = next_btn.first
                # Check if disabled
                disabled = await first.get_attribute("disabled")
                cls = await first.get_attribute("class") or ""
                if disabled or "k-disabled" in cls or "disabled" in cls.lower():
                    return False

                # Check parent for disabled state
                parent_cls = await first.evaluate("el => el.parentElement?.className || ''")
                if "k-disabled" in parent_cls:
                    return False

                await first.click()
                await self.page.wait_for_timeout(3_000)
                return True
        except Exception:
            pass
        return False
