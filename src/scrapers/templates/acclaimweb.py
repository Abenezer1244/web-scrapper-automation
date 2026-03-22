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
        _logger.info("Page URL: %s", self.page.url)
        _logger.info("Page title: %s", await self.page.title())

        try:
            # Log page content for debugging
            body_text = await self.page.inner_text("body")
            _logger.info("Page text (first 500): %s", body_text[:500].replace('\n', ' '))

            # AcclaimWeb disclaimer: look for accept/continue links
            clicked = await self.page.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a, button, input[type="button"], input[type="submit"]');
                    const found = [];
                    for (const el of links) {
                        const text = (el.textContent || el.value || '').trim().toLowerCase();
                        if (text.length > 0 && text.length < 50) found.push(text);
                        if (text.includes('accept') || text.includes('agree') ||
                            text.includes('acknowledge') || text.includes('continue') ||
                            text.includes('public') || text.includes('search')) {
                            el.click();
                            return text;
                        }
                    }
                    return 'NO_MATCH:' + found.slice(0, 10).join('|');
                })()
            """)
            if clicked and not clicked.startswith('NO_MATCH'):
                await self.page.wait_for_timeout(3_000)
                _logger.info("Disclaimer accepted: %s", clicked)
                _logger.info("After disclaimer URL: %s", self.page.url)
            else:
                _logger.info("No disclaimer match found. Links: %s", clicked)
        except Exception as exc:
            _logger.info("Disclaimer error: %s", str(exc)[:100])

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
        """Fill the Kendo DatePicker date inputs."""
        _logger.info("Fill dates page URL: %s", self.page.url)
        try:
            await self.page.wait_for_timeout(3_000)

            # Log what elements exist on page
            page_info = await self.page.evaluate("""
                (() => {
                    const info = {
                        has_jquery: typeof $ !== 'undefined',
                        inputs: [],
                        selects: [],
                        buttons: [],
                    };
                    document.querySelectorAll('input').forEach(el => {
                        info.inputs.push({id: el.id, name: el.name, type: el.type, value: el.value});
                    });
                    document.querySelectorAll('select').forEach(el => {
                        info.selects.push({id: el.id, name: el.name});
                    });
                    document.querySelectorAll('button, input[type="submit"]').forEach(el => {
                        info.buttons.push({id: el.id, text: (el.textContent || el.value || '').trim()});
                    });
                    return info;
                })()
            """)
            _logger.info("Page elements: jQuery=%s inputs=%d selects=%d buttons=%d",
                         page_info.get('has_jquery'), len(page_info.get('inputs', [])),
                         len(page_info.get('selects', [])), len(page_info.get('buttons', [])))
            for inp in page_info.get('inputs', [])[:10]:
                _logger.info("  input: id=%s name=%s type=%s val=%s", inp.get('id'), inp.get('name'), inp.get('type'), inp.get('value', '')[:30])
            for btn in page_info.get('buttons', []):
                _logger.info("  button: id=%s text=%s", btn.get('id'), btn.get('text', '')[:30])

            # Try Kendo DatePicker API first
            filled = await self.page.evaluate(f"""
                (() => {{
                    if (typeof $ === 'undefined') return 'no_jquery';

                    // Try setting date range dropdown first
                    const dd = $('#DateRangeDropDown').data('kendoDropDownList');
                    if (dd) {{
                        dd.value('SpecificDateRange');
                        dd.trigger('change');
                    }}

                    const fromPicker = $('#FromDatePicker').data('kendoDatePicker');
                    const toPicker = $('#ToDatePicker').data('kendoDatePicker');
                    if (fromPicker && toPicker) {{
                        fromPicker.value('{date_from}');
                        fromPicker.trigger('change');
                        toPicker.value('{date_to}');
                        toPicker.trigger('change');
                        return 'kendo_ok';
                    }}

                    const singlePicker = $('#RecordDatePicker').data('kendoDatePicker');
                    if (singlePicker) {{
                        singlePicker.value('{date_from}');
                        singlePicker.trigger('change');
                        return 'kendo_single';
                    }}

                    return 'no_kendo_pickers';
                }})()
            """)
            _logger.info("Kendo fill result: %s", filled)

            if filled in ('kendo_ok', 'kendo_single'):
                await self.page.wait_for_timeout(1_000)
                return

            # Fallback: type into input elements directly
            for from_id, to_id in [
                ("FromDatePicker", "ToDatePicker"),
                ("RecordDateFrom", "RecordDateTo"),
                ("txtStartDate", "txtEndDate"),
                ("StartDate", "EndDate"),
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
                    _logger.info("Dates typed via fallback: %s to %s (ids: %s, %s)", date_from, date_to, from_id, to_id)
                    return

            # Last resort: find any date-looking inputs
            date_inputs = await self.page.locator("input[type='text'], input[type='date']").all()
            _logger.info("Found %d text/date inputs for fallback", len(date_inputs))
            for inp in date_inputs[:2]:
                val = await inp.get_attribute("placeholder") or ""
                _logger.info("  placeholder=%s", val)

            _logger.warning("Could not find date inputs on page")

        except Exception as exc:
            _logger.warning("Could not set dates: %s", str(exc)[:120])

    async def _submit_search(self) -> None:
        """Click the Search button and wait for results grid to populate."""
        try:
            # Find the search button
            search_btn = self.page.locator("#SearchBtn")
            btn_count = await search_btn.count()
            _logger.info("SearchBtn count: %d", btn_count)

            if btn_count == 0:
                search_btn = self.page.locator(
                    "button:has-text('Search'), input[type='submit'][value='Search']"
                )
                btn_count = await search_btn.count()
                _logger.info("Fallback search button count: %d", btn_count)

            if btn_count == 0:
                _logger.warning("No search button found!")
                body = await self.page.inner_text("body")
                _logger.info("Page body (500 chars): %s", body[:500].replace('\n', ' '))
                return

            await search_btn.first.click()
            _logger.info("Search button clicked, waiting for results...")

            # Wait for results grid to populate (Kendo Grid loads via AJAX)
            try:
                await self.page.wait_for_selector(
                    "#SearchResultGrid tbody tr, .k-grid-content tr, .no-results, .k-grid-norecords",
                    timeout=60_000,
                )
                _logger.info("Results selector found")
            except Exception:
                await self.page.wait_for_timeout(5_000)
                _logger.info("Results selector timeout, continuing anyway")

            await self.page.wait_for_timeout(2_000)

            # Log page state after search
            body = await self.page.inner_text("body")
            _logger.info("After search URL: %s", self.page.url)
            _logger.info("After search text (500): %s", body[:500].replace('\n', ' '))

        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:120])

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
