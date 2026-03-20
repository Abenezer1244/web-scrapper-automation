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
            btn = self.page.locator("button:has-text('I Acknowledge'), input[value*='Acknowledge']")
            if await btn.count() > 0:
                await btn.first.click()
                await self.page.wait_for_timeout(2_000)
                _logger.info("Disclaimer accepted")
        except Exception:
            _logger.info("No disclaimer found, continuing")

    async def _configure_search(self, record_type: str, date_from: str, date_to: str) -> None:
        """Configure EagleWeb search form."""
        # Uncheck "Search All Types" if filtering by document type
        if record_type != "all":
            try:
                all_types_cb = self.page.locator("input[type='checkbox']").filter(has_text="Search All Types")
                if await all_types_cb.count() == 0:
                    # Try by nearby text
                    all_types_cb = self.page.locator("text=Search All Types >> .. >> input[type='checkbox']")

                if await all_types_cb.count() > 0 and await all_types_cb.first.is_checked():
                    await all_types_cb.first.uncheck()
                    await self.page.wait_for_timeout(1_000)
                    _logger.info("Unchecked 'Search All Types'")

                    # Select relevant document type checkboxes
                    await self._select_doc_types(record_type)
            except Exception as exc:
                _logger.warning("Could not configure doc types: %s", str(exc)[:60])

        # Fill date range
        try:
            # EagleWeb has Start Date and End Date text inputs
            start_inputs = self.page.locator("input[type='text']").all()
            date_fields = []
            for inp in await self.page.locator("input[type='text']").all():
                val = await inp.get_attribute("value") or ""
                # Date fields typically have date-like default values
                if "/" in val and len(val) >= 8:
                    date_fields.append(inp)

            if len(date_fields) >= 2:
                await date_fields[0].triple_click()
                await date_fields[0].fill(date_from)
                await date_fields[1].triple_click()
                await date_fields[1].fill(date_to)
                _logger.info("Date range set: %s to %s", date_from, date_to)
            else:
                # Fallback: use JavaScript to find and fill date inputs
                await self.page.evaluate(f"""
                    (() => {{
                        const inputs = document.querySelectorAll('input[type="text"]');
                        for (const inp of inputs) {{
                            if (inp.value && inp.value.includes('/') && inp.value.length >= 8) {{
                                if (inp.value.startsWith('01/01')) {{
                                    inp.value = '{date_from}';
                                    inp.dispatchEvent(new Event('change'));
                                }} else {{
                                    inp.value = '{date_to}';
                                    inp.dispatchEvent(new Event('change'));
                                }}
                            }}
                        }}
                    }})()
                """)
                _logger.info("Date range set via JS: %s to %s", date_from, date_to)
        except Exception as exc:
            _logger.warning("Could not set date range: %s", str(exc)[:60])

    async def _select_doc_types(self, record_type: str) -> None:
        """Select document type checkboxes matching the record type."""
        keywords = _DOC_TYPE_MAP.get(record_type, [])
        if not keywords:
            return

        # Get all checkbox labels
        checkboxes = await self.page.locator("input[type='checkbox']").all()
        selected = 0
        for cb in checkboxes:
            label_text = ""
            # Try to get label from parent or sibling
            try:
                parent = cb.locator("..")
                label_text = (await parent.inner_text()).strip().upper()
            except Exception:
                pass

            for keyword in keywords:
                if keyword in label_text:
                    if not await cb.is_checked():
                        await cb.check()
                        selected += 1
                        _logger.info("Selected doc type: %s", label_text[:40])
                    break

        if selected == 0:
            _logger.warning("No doc type checkboxes matched for %s", record_type)

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
        """Extract records from the current results page."""
        records: list[ScrapedRecord] = []

        try:
            # EagleWeb results are in table rows with class 'searchResultRow' or similar
            soup = await self.get_soup()

            # Find the results table — EagleWeb uses tables with specific patterns
            results_table = None
            for table in soup.find_all("table"):
                headers = table.find_all("th")
                header_text = " ".join(h.get_text() for h in headers).upper()
                if "GRANTOR" in header_text or "GRANTEE" in header_text or "RECORDING" in header_text:
                    results_table = table
                    break

            if not results_table:
                # Try finding by result count text
                page_text = await self.page.inner_text("body")
                if "No documents found" in page_text or "0 documents" in page_text:
                    _logger.info("No results found on this page")
                    return []
                _logger.warning("Could not find results table")
                return []

            rows = results_table.find_all("tr")
            # Skip header row(s)
            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue

                cell_texts = [c.get_text(strip=True) for c in cells]

                # EagleWeb typical columns:
                # Recording Date | Doc Type | Grantor | Grantee | Legal | Related Docs
                record = ScrapedRecord()

                for i, text in enumerate(cell_texts):
                    text_upper = text.upper()

                    # Date detection (MM/DD/YYYY)
                    import re
                    date_match = re.match(r"\d{1,2}/\d{1,2}/\d{4}", text)
                    if date_match and not record.date_recorded:
                        record.date_recorded = date_match.group()
                        continue

                    # Parcel ID detection (numeric patterns)
                    parcel_match = re.search(r"\b\d{10,}\b", text)
                    if parcel_match and not record.parcel_id:
                        record.parcel_id = parcel_match.group()

                    # Name detection (all caps, likely Grantor/Grantee)
                    if text and text == text.upper() and len(text) > 3 and not text.isdigit():
                        if not record.party_name:
                            record.party_name = text
                        elif text != record.party_name:
                            # Could be heirs or second party
                            if record.heirs:
                                record.heirs += f", {text}"
                            else:
                                record.heirs = text

                    # Legal description
                    legal_keywords = ["LOT", "BLOCK", "SEC", "TWP", "ADD", "PLAT", "SUB"]
                    if any(kw in text_upper for kw in legal_keywords):
                        record.legal_description = text

                if record.party_name or record.date_recorded:
                    records.append(record)

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
