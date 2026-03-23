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
        self._single_date_mode = False  # Set by _fill_dates when only 1 date input exists

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
            # Use 1-day chunks in single-date mode, 7-day chunks otherwise
            effective_days = 1 if self._single_date_mode else chunk_days
            chunk_end = min(chunk_start + timedelta(days=effective_days), end)
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

            chunk_start = chunk_end
            await self.polite_delay()

        # Enrich records with parcel data
        enrichable = [r for r in all_records if r.parcel_id and len(r.parcel_id) >= 8]
        if enrichable:
            _logger.info("Enriching %d records with parcel data", len(enrichable))
            from src.scrapers.enrichment import enrich_parcel

            for record in enrichable:
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
        """Click disclaimer accept link/button/checkbox if present.

        AcclaimWeb sites vary:
        - Some have an input[type='submit'] button (e.g. Pend Oreille)
        - Some have a checkbox labeled 'Accept Disclaimer' (e.g. Chelan)
        - Some have a link with 'accept'/'agree' text
        """
        _logger.info("Page URL: %s", self.page.url)
        _logger.info("Page title: %s", await self.page.title())

        try:
            body_text = await self.page.inner_text("body")
            _logger.info("Page text (first 500): %s", body_text[:500].replace('\n', ' '))

            # Strategy 1: Checkbox-based disclaimer (Chelan pattern)
            # Some AcclaimWeb sites use a checkbox labeled "Accept Disclaimer"
            accept_checkbox = self.page.locator(
                "input[type='checkbox']"
            )
            checkbox_count = await accept_checkbox.count()
            if checkbox_count > 0:
                for i in range(checkbox_count):
                    cb = accept_checkbox.nth(i)
                    # Check label text or nearby text
                    cb_id = await cb.get_attribute("id") or ""
                    cb_name = await cb.get_attribute("name") or ""
                    parent_text = await cb.evaluate(
                        "el => (el.parentElement?.textContent || el.closest('label')?.textContent || '').trim()"
                    )
                    _logger.info("Checkbox %d: id=%s name=%s label=%s", i, cb_id, cb_name, parent_text[:60])

                    if any(kw in (cb_id + cb_name + parent_text).lower()
                           for kw in ["accept", "disclaim", "agree", "acknowledge"]):
                        if not await cb.is_checked():
                            await cb.check()
                            _logger.info("Checked disclaimer checkbox: %s", cb_id or cb_name)

                        # After checking, look for a submit/continue button
                        await self.page.wait_for_timeout(1_000)
                        submit_after = self.page.locator(
                            "input[type='submit'], button[type='submit'], "
                            "button:has-text('Continue'), button:has-text('Search'), "
                            "a:has-text('Continue'), a:has-text('Search')"
                        )
                        if await submit_after.count() > 0:
                            _logger.info("Clicking submit after checkbox")
                            try:
                                async with self.page.expect_navigation(timeout=15_000):
                                    await submit_after.first.click()
                                _logger.info("Disclaimer accepted via checkbox + submit")
                            except Exception:
                                await self.page.wait_for_timeout(3_000)
                                _logger.info("Disclaimer checkbox + submit (no nav event)")
                        else:
                            # Checkbox alone may trigger JS navigation
                            await self.page.wait_for_timeout(3_000)
                            _logger.info("Disclaimer checkbox checked (no submit button found)")

                        _logger.info("After disclaimer URL: %s", self.page.url)
                        return

            # Strategy 2: Submit button (standard pattern)
            accept_btn = self.page.locator(
                "input[type='submit'][value*='accept' i], "
                "input[type='submit'][value*='agree' i], "
                "input[type='submit'][value*='acknowledge' i], "
                "button:has-text('accept'), button:has-text('agree'), "
                "a:has-text('accept'), a:has-text('agree'), "
                "a:has-text('continue'), a:has-text('public')"
            )
            if await accept_btn.count() > 0:
                btn_val = await accept_btn.first.get_attribute("value") or await accept_btn.first.inner_text()
                _logger.info("Found disclaimer button: %s", btn_val[:60])

                try:
                    async with self.page.expect_navigation(timeout=15_000):
                        await accept_btn.first.click()
                    _logger.info("Disclaimer accepted via navigation")
                except Exception:
                    await self.page.wait_for_timeout(3_000)
                    _logger.info("Disclaimer clicked (no navigation event)")

                _logger.info("After disclaimer URL: %s", self.page.url)
            else:
                _logger.info("No disclaimer button or checkbox found on page")
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

            # Fallback: type into paired input elements directly
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

            # Single date field fallback (e.g. Douglas AcclaimWeb has only #RecordDate)
            # Type the start date — the search returns records ON that date.
            # The chunking loop will iterate day-by-day for single-date sites.
            single_el = self.page.locator("#RecordDate")
            if await single_el.count() > 0:
                await single_el.click()
                await single_el.fill("")
                await single_el.press_sequentially(date_from, delay=30)
                _logger.info("Single date typed: %s (RecordDate field)", date_from)
                self._single_date_mode = True
                return

            # Last resort: find any visible date-looking text inputs
            date_inputs = await self.page.locator(
                "input[type='text']:visible, input[type='date']:visible"
            ).all()
            if date_inputs:
                _logger.info("Found %d visible date inputs for last-resort fill", len(date_inputs))
                await date_inputs[0].click()
                await date_inputs[0].fill("")
                await date_inputs[0].press_sequentially(date_from, delay=30)
                if len(date_inputs) >= 2:
                    await date_inputs[1].click()
                    await date_inputs[1].fill("")
                    await date_inputs[1].press_sequentially(date_to, delay=30)
                    _logger.info("Dates typed via last-resort: %s to %s", date_from, date_to)
                else:
                    _logger.info("Single date typed via last-resort: %s", date_from)
                    self._single_date_mode = True
                return

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
                    "#SearchResultGrid tbody tr, .k-grid-content tr, "
                    ".no-results, .k-grid-norecords, "
                    "table.k-grid tbody tr, [data-role='grid'] tbody tr",
                    timeout=60_000,
                )
                _logger.info("Results selector found")
            except Exception:
                await self.page.wait_for_timeout(5_000)
                _logger.info("Results selector timeout, continuing anyway")

            # Extra wait for Kendo AJAX to fully populate grid data
            await self.page.wait_for_timeout(3_000)

            # Wait for any Kendo loading indicator to disappear
            try:
                loading = self.page.locator(".k-loading-mask, .k-loading-image")
                if await loading.count() > 0:
                    await loading.first.wait_for(state="hidden", timeout=10_000)
                    _logger.info("Kendo loading indicator cleared")
            except Exception:
                pass

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
            # First, log grid diagnostic info
            diag = await self.page.evaluate("""
                (() => {
                    const d = {
                        has_jquery: typeof $ !== 'undefined',
                        grid_el: !!document.querySelector('#SearchResultGrid'),
                        grid_rows: document.querySelectorAll('#SearchResultGrid tbody tr, .k-grid-content tbody tr').length,
                        all_tables: document.querySelectorAll('table').length,
                        all_trs: document.querySelectorAll('table tbody tr').length,
                    };
                    if (d.has_jquery) {
                        const grid = $('#SearchResultGrid').data('kendoGrid');
                        d.has_kendo_grid = !!grid;
                        if (grid) {
                            const view = grid.dataSource.view();
                            d.kendo_view_len = view ? view.length : 0;
                            d.kendo_total = grid.dataSource.total();
                            if (view && view.length > 0) {
                                d.first_row_keys = Object.keys(view[0]).filter(k => !k.startsWith('_'));
                            }
                        }
                    }
                    return d;
                })()
            """)
            _logger.info("Grid diagnostics: %s", diag)

            raw = await self.page.evaluate("""
                (() => {
                    // Try Kendo Grid data first (most reliable)
                    if (typeof $ !== 'undefined') {
                        const grid = $('#SearchResultGrid').data('kendoGrid');
                        if (grid) {
                            const data = grid.dataSource.view();
                            if (data && data.length > 0) {
                                return Array.from(data).map(item => ({
                                    instrument: item.InstrumentNumber || item.Instrument || item.AFN || item.instrumentNumber || '',
                                    date_recorded: item.RecordDate || item.RecordingDate || item.recordDate || item.RecordedDate || '',
                                    doc_type: item.DocType || item.DocumentType || item.DocTypeName || item.docType || item.DocumentDescription || '',
                                    grantor: item.Grantor || item.GrantorName || item.grantor || item.DirectName || '',
                                    grantee: item.Grantee || item.GranteeName || item.grantee || item.IndirectName || '',
                                    legal: item.LegalDescription || item.Legal || item.legalDescription || '',
                                    parcel: item.ParcelId || item.Parcel || item.APN || item.parcelId || '',
                                }));
                            }
                        }

                        // Try alternate grid selectors
                        const altGrids = ['.k-grid', '[data-role="grid"]', '#resultsGrid', '#searchResults'];
                        for (const sel of altGrids) {
                            const el = $(sel).data('kendoGrid');
                            if (el) {
                                const data = el.dataSource.view();
                                if (data && data.length > 0) {
                                    return Array.from(data).map(item => {
                                        const keys = Object.keys(item).filter(k => !k.startsWith('_'));
                                        return {
                                            instrument: item[keys.find(k => /instrument|afn/i.test(k))] || '',
                                            date_recorded: item[keys.find(k => /date|record/i.test(k))] || '',
                                            doc_type: item[keys.find(k => /doc.*type|document/i.test(k))] || '',
                                            grantor: item[keys.find(k => /grantor|direct/i.test(k))] || '',
                                            grantee: item[keys.find(k => /grantee|indirect/i.test(k))] || '',
                                            legal: item[keys.find(k => /legal/i.test(k))] || '',
                                            parcel: item[keys.find(k => /parcel|apn/i.test(k))] || '',
                                        };
                                    });
                                }
                            }
                        }
                    }

                    // Fallback: extract from DOM table rows
                    const rows = document.querySelectorAll(
                        '#SearchResultGrid tbody tr, .k-grid-content tbody tr, table.k-grid tbody tr'
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
