"""Pierce County, WA — Probate records connector.

Source: ARMS Web (armsweb.co.pierce.wa.us)
Record type: probate

ARMS results table columns (0-indexed):
  0: Row #
  1: Image link
  2: Select checkbox
  3: Instrument # / Book-Page
  4: Date Recorded
  5: Document Type
  6: Name ([R] recording party) + Associated Name ([E] heir/executor)
  7: Legal Description (may contain embedded parcel IDs)
  8: Status
"""

import re

from bs4 import BeautifulSoup, Tag

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment import enrich_parcel
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.pierce_wa_probate")

# Register approved domains for SSRF allowlist
add_scrape_domain("armsweb.co.pierce.wa.us")

# ─── Patterns ─────────────────────────────────────────────────────────────────

_ARMS_HOME = "https://armsweb.co.pierce.wa.us/"
_ARMS_SEARCH = "https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx"


class PierceWAProbateScraper(BridgeScraper):
    """Scrapes probate filings from Pierce County ARMS Web portal."""

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        _logger.info("Pierce WA Probate — scraping %s to %s", date_from, date_to)

        await self._accept_disclaimer()
        await self.navigate(_ARMS_SEARCH)
        await self._fill_search_form(date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0

        while True:
            page_num += 1
            _logger.info("Processing page %d", page_num)

            soup = await self.get_soup_async()
            page_records = self._extract_records(soup)

            new_count = 0
            for record in page_records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info("Page %d — %d new records (total: %d)", page_num, new_count, len(all_records))

            if not await self._go_to_next_page():
                break

            await self.polite_delay()

        # ── Fetch real parcel IDs from detail pages ─────────────────────────
        _logger.info("Fetching parcel IDs from detail pages for %d records...", len(all_records))
        await self._fetch_parcel_ids(all_records)
        parcels_found = sum(1 for r in all_records if r.parcel_id)
        _logger.info("Parcel IDs found: %d/%d", parcels_found, len(all_records))

        # ── Enrichment pass ───────────────────────────────────────────────────
        _logger.info("Enriching %d records with parcel data", len(all_records))
        for record in all_records:
            if record.parcel_id:
                enriched = await enrich_parcel(record.parcel_id, "pierce", "WA")
                record.property_address = enriched.get("property_address") or record.property_address
                record.mailing_address = enriched.get("mailing_address") or record.mailing_address
                record.enrichment_data = enriched
                await self.polite_delay()

        _logger.info("Pierce WA Probate — complete. %d records", len(all_records))
        return all_records

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _accept_disclaimer(self) -> None:
        """Navigate to the ARMS home page and accept the terms disclaimer."""
        page = self.page
        await self.navigate(_ARMS_HOME)

        accept_link = page.locator("a:has-text('Click here to acknowledge')")
        try:
            await accept_link.wait_for(timeout=10_000)
            await accept_link.click()
            await page.wait_for_load_state("load")
            _logger.info("Disclaimer accepted")
        except Exception:
            _logger.info("No disclaimer prompt found — may already be accepted")

    async def _fill_search_form(self, date_from: str, date_to: str) -> None:
        """Fill and submit the ARMS Web search form."""
        page = self.page
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(1_000)

        # Check PROBATE checkbox
        probate_cb = page.get_by_role("checkbox", name="PROBATE")
        try:
            await probate_cb.scroll_into_view_if_needed(timeout=10_000)
            await probate_cb.check(timeout=5_000)
            _logger.info("Checked PROBATE document type")
        except Exception:
            _logger.warning("Could not check PROBATE via locator, trying JS fallback")
            await page.evaluate("""
                () => {
                    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of checkboxes) {
                        const label = cb.nextSibling;
                        if (label && label.textContent && label.textContent.trim() === 'PROBATE') {
                            cb.checked = true;
                            cb.dispatchEvent(new Event('change', { bubbles: true }));
                            break;
                        }
                    }
                }
            """)
            _logger.info("Checked PROBATE via JS fallback")

        # Fill date range via JS
        await page.evaluate("""
            ([dateFrom, dateTo]) => {
                const inputs = document.querySelectorAll('input[title="mm/dd/yyyy"]');
                if (inputs.length >= 2) {
                    inputs[0].value = dateFrom;
                    inputs[0].dispatchEvent(new Event('change', { bubbles: true }));
                    inputs[1].value = dateTo;
                    inputs[1].dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    const altInputs = document.querySelectorAll('input[alt="mm/dd/yyyy"]');
                    if (altInputs.length >= 2) {
                        altInputs[0].value = dateFrom;
                        altInputs[0].dispatchEvent(new Event('change', { bubbles: true }));
                        altInputs[1].value = dateTo;
                        altInputs[1].dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
            }
        """, [date_from, date_to])
        _logger.info("Filled date range: %s — %s", date_from, date_to)

        # Submit
        search_btn = page.get_by_role("button", name="Search", exact=True).first
        await search_btn.scroll_into_view_if_needed(timeout=5_000)
        await search_btn.click(timeout=10_000)
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(3_000)
        _logger.info("Search form submitted for %s — %s", date_from, date_to)

    async def _go_to_next_page(self) -> bool:
        """Click the Next page button. Returns True if navigated."""
        page = self.page

        # ARMS uses <button> elements for pagination, not <a> links
        next_btn = page.get_by_role("button", name="Next", exact=True).first
        try:
            if await next_btn.count() == 0:
                return False
            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled:
                return False
            await next_btn.click(timeout=10_000)
            await page.wait_for_load_state("load")
            await page.wait_for_timeout(2_000)
            _logger.info("Navigated to next page")
            return True
        except Exception as exc:
            _logger.warning("Next page navigation failed: %s", exc)
            return False

    async def _fetch_parcel_ids(self, records: list[ScrapedRecord]) -> None:
        """Click into each record's detail page to get the real parcel ID.

        The ARMS detail page has a 'Legal Description' tab that shows the
        actual assessor parcel ID (not visible in search results).

        Uses the instrument number dropdown on the detail page to navigate
        between records without going back to search results.
        """
        page = self.page

        # Get records that need parcel IDs (skip ones that already have one)
        needs_parcel = [
            r for r in records
            if not r.parcel_id and r.enrichment_data and r.enrichment_data.get("instrument_number")
        ]

        if not needs_parcel:
            _logger.info("All records already have parcel IDs or no instrument numbers")
            return

        _logger.info("Fetching parcel IDs for %d records from detail pages", len(needs_parcel))

        # Click the first instrument number to enter the detail view
        first_inst = needs_parcel[0].enrichment_data["instrument_number"]
        try:
            inst_link = page.locator(f"text={first_inst}").first
            await inst_link.click(timeout=10_000)
            await page.wait_for_load_state("load")
            await page.wait_for_timeout(1_000)
        except Exception as exc:
            _logger.warning("Could not click into detail page: %s", str(exc)[:60])
            return

        # Now iterate through records using the instrument dropdown
        for i, record in enumerate(needs_parcel):
            inst_num = record.enrichment_data["instrument_number"]

            try:
                # Select this instrument from the dropdown
                dropdown = page.locator("select").first
                if await dropdown.count() > 0:
                    await dropdown.select_option(value=inst_num, timeout=5_000)
                    await page.wait_for_load_state("load")
                    await page.wait_for_timeout(500)

                # Click "Legal Description" tab
                legal_tab = page.locator("text=Legal Description").first
                if await legal_tab.count() > 0:
                    await legal_tab.click(timeout=5_000)
                    await page.wait_for_timeout(500)

                # Extract parcel ID from the detail page
                parcel_text = await page.evaluate("""() => {
                    const cells = document.querySelectorAll('td');
                    for (let i = 0; i < cells.length; i++) {
                        if (cells[i].textContent.trim() === 'Parcel Id:' && cells[i+1]) {
                            return cells[i+1].textContent.trim();
                        }
                    }
                    return null;
                }""")

                if parcel_text and parcel_text.strip():
                    record.parcel_id = parcel_text.strip()
                    if (i + 1) <= 5 or (i + 1) % 20 == 0:
                        _logger.info("  %s → parcel %s", inst_num, record.parcel_id)

            except Exception as exc:
                _logger.warning("  Detail page failed for %s: %s", inst_num, str(exc)[:50])

        # Navigate back to results
        try:
            back_link = page.locator("text=Back to Results").first
            if await back_link.count() > 0:
                await back_link.click(timeout=5_000)
                await page.wait_for_load_state("load")
        except Exception:
            pass

    def _extract_records(self, soup: BeautifulSoup) -> list[ScrapedRecord]:
        """Extract records from the ARMS results table.

        The ARMS page uses nested tables with many td cells per row (~39).
        The data table is identified by: 20+ rows, first data row starts with
        a numeric row number, and has many td cells.
        """
        tables = soup.find_all("table")
        data_table = None
        for t in tables:
            data_rows = t.find_all("tr")
            if len(data_rows) < 5:
                continue
            # Check if the second row starts with a number (row counter)
            if len(data_rows) > 1:
                first_td = data_rows[1].find("td")
                if first_td and first_td.get_text(strip=True).isdigit():
                    data_table = t
                    break

        if data_table is None:
            _logger.warning("No results data table found on page")
            return []

        rows = data_table.find_all("tr")
        records: list[ScrapedRecord] = []

        # Skip header row
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 9:
                continue

            # First cell should be a row number
            first_text = cells[0].get_text(strip=True)
            if not first_text.isdigit():
                continue

            record = self._map_row_by_text(cells)
            if record:
                records.append(record)

        return records

    def _map_row_by_text(self, cells: list[Tag]) -> ScrapedRecord | None:
        """Map a table row by extracting text from all cells and parsing.

        Since ARMS uses nested tables with varying td counts (~39 per row),
        we extract all cell text and find fields by content patterns.
        """
        # Gather all cell texts
        all_texts = []
        for c in cells:
            text = self.clean(c.get_text(separator=" ", strip=True))
            if text:
                all_texts.append(text)

        if not all_texts:
            return None

        record = ScrapedRecord()

        # Find instrument number (12-digit, starts with year)
        inst_re = re.compile(r"\b(20\d{10})\b")
        for text in all_texts:
            m = inst_re.search(text)
            if m:
                # Store in enrichment_data for later detail page lookup
                record.enrichment_data = {"instrument_number": m.group(1)}
                break

        # Find date (MM/DD/YYYY pattern)
        date_re = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
        for text in all_texts:
            m = date_re.search(text)
            if m:
                record.date_recorded = m.group()
                break

        # Find the Name cell — contains [R] and/or [E] markers
        for c in cells:
            cell_text = c.get_text(separator="|", strip=True)
            if "[R]" in cell_text or "[E]" in cell_text:
                party_name, heirs = self._parse_name_cell(c)
                record.party_name = party_name
                record.heirs = heirs
                break

        # Find Legal Description — contains land keywords (LOT, BLK, SEC, etc.)
        legal_re = re.compile(r"\b(LT|LOT|BLK|BLOCK|SEC|TWNSHP|RNG|ADDN|PLAT|DIV|SHORT PLAT)\b", re.IGNORECASE)
        for text in all_texts:
            if legal_re.search(text) and text != record.party_name:
                record.legal_description = text
                break

        # Extract parcel ID: exactly 10 digits (Pierce County format)
        # Only search in legal description text to avoid matching instrument numbers
        parcel_10_re = re.compile(r"\b(\d{10})\b")
        if record.legal_description:
            m = parcel_10_re.search(record.legal_description)
            if m:
                record.parcel_id = m.group(1)

        # Skip empty rows
        if not record.date_recorded and not record.party_name:
            return None

        # Skip garbage
        if record.party_name and len(record.party_name) > 200:
            return None
        junk = ["Page 1", "Sort By", "New Search", "Criteria:", "records found", "Select All"]
        if record.party_name and any(kw in record.party_name for kw in junk):
            return None

        return record

    @staticmethod
    def _parse_name_cell(cell: Tag) -> tuple[str | None, str | None]:
        """Parse the Name/Associated Name cell into (party_name, heirs).

        The cell structure is:
          [R] ESTATE_NAME EST OF
          [E] HEIR_NAME HEIRS OF

        Where [R] = Recording party, [E] = Associated name (heir/executor).
        These are in separate text nodes or child <span>/<div> elements.
        """
        # Get all text segments from the cell
        text_parts = []
        for child in cell.children:
            if isinstance(child, Tag):
                text_parts.append(child.get_text(strip=True))
            elif isinstance(child, str):
                stripped = child.strip()
                if stripped:
                    text_parts.append(stripped)

        # Join and split by [R] and [E] markers
        full_text = " ".join(text_parts)

        party_name = None
        heirs = None

        # Extract [R] portion (recording party / estate)
        r_match = re.search(r"\[R\]\s*(.+?)(?=\s*\[E\]|$)", full_text)
        if r_match:
            party_name = r_match.group(1).strip()

        # Extract [E] portion (associated name / heir)
        e_match = re.search(r"\[E\]\s*(.+?)$", full_text)
        if e_match:
            heirs = e_match.group(1).strip()

        # Fallback: if no [R]/[E] markers, use full text as party_name
        if not party_name and not heirs:
            cleaned = full_text.strip()
            if cleaned:
                party_name = cleaned

        return party_name, heirs
