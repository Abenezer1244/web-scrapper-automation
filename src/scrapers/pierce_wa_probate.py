"""Pierce County, WA — Probate records connector.

Source: ARMS Web (armsweb.co.pierce.wa.us)
Record type: probate

The site requires:
1. Accept disclaimer at the landing page
2. Navigate to /RealEstate/SearchEntry.aspx
3. Check the PROBATE document type checkbox
4. Fill date range fields
5. Click the Search button

Extraction uses heuristics, not hardcoded column indices, so the scraper
stays functional if the portal reorders its columns.
"""

import re

from bs4 import BeautifulSoup

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment import enrich_parcel
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.pierce_wa_probate")

# Register approved domains for SSRF allowlist
add_scrape_domain("armsweb.co.pierce.wa.us")

# ─── Heuristic patterns ───────────────────────────────────────────────────────

_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
_PARCEL_RE = re.compile(r"\b\d{10}\b")
_LEGAL_KEYWORDS = re.compile(r"\b(LOT|BLOCK|SEC|TWP|RNG|PLAT|TRACT|SUBDIVISION)\b", re.IGNORECASE)

_ARMS_HOME = "https://armsweb.co.pierce.wa.us/"
_ARMS_SEARCH = "https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx"


class PierceWAProbateScraper(BridgeScraper):
    """Scrapes probate filings from Pierce County ARMS Web portal."""

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run full probate scrape for the given date range.

        Args:
            date_from: Start date in MM/DD/YYYY format.
            date_to:   End date in MM/DD/YYYY format.

        Returns:
            List of ScrapedRecord instances, deduplicated by row hash.
        """
        _logger.info("Pierce WA Probate — scraping %s to %s", date_from, date_to)

        # Step 1: Accept the disclaimer to establish a session
        await self._accept_disclaimer()

        # Step 2: Navigate to the search form
        await self.navigate(_ARMS_SEARCH)

        # Step 3: Fill and submit the search form
        await self._fill_search_form(date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0

        while True:
            page_num += 1
            _logger.info("Processing page %d", page_num)

            soup = await self.get_soup_async()
            page_records = self._extract_records(soup)

            for record in page_records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)

            _logger.info("Page %d — %d new records (total: %d)", page_num, len(page_records), len(all_records))

            if not await self._go_to_next_page():
                break

            await self.polite_delay()

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

        # Look for the disclaimer acceptance link
        accept_link = page.locator("a:has-text('Click here to acknowledge')")
        try:
            await accept_link.wait_for(timeout=10_000)
            await accept_link.click()
            await page.wait_for_load_state("load")
            _logger.info("Disclaimer accepted")
        except Exception:
            # Disclaimer may already be accepted (session cookie present)
            _logger.info("No disclaimer prompt found — may already be accepted")

    async def _fill_search_form(self, date_from: str, date_to: str) -> None:
        """Fill and submit the ARMS Web search form."""
        page = self.page

        # Wait for the form to be ready
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(1_000)  # Let JS initialize

        # Check the PROBATE document type checkbox
        # The document type list is a long scrollable checkbox list.
        # Use role-based locator for reliability.
        probate_cb = page.get_by_role("checkbox", name="PROBATE")
        try:
            await probate_cb.scroll_into_view_if_needed(timeout=10_000)
            await probate_cb.check(timeout=5_000)
            _logger.info("Checked PROBATE document type")
        except Exception:
            # Fallback: evaluate JS to check the checkbox directly
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

        # Fill date range fields via JS — Infragistics date pickers redraw on
        # focus/change, so direct DOM manipulation is more reliable.
        await page.evaluate("""
            ([dateFrom, dateTo]) => {
                const inputs = document.querySelectorAll('input[title="mm/dd/yyyy"]');
                if (inputs.length >= 2) {
                    inputs[0].value = dateFrom;
                    inputs[0].dispatchEvent(new Event('change', { bubbles: true }));
                    inputs[1].value = dateTo;
                    inputs[1].dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    // Fallback: find by alt attribute
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

        # Submit the form — click the Search button (use role-based locator)
        search_btn = page.get_by_role("button", name="Search", exact=True).first
        await search_btn.scroll_into_view_if_needed(timeout=5_000)
        await search_btn.click(timeout=10_000)
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(3_000)  # Wait for results to render
        _logger.info("Search form submitted for %s — %s", date_from, date_to)

    async def _go_to_next_page(self) -> bool:
        """Click the next page control if one exists. Returns True if navigated."""
        page = self.page

        # Look for a "Next" link or pagination button
        next_selectors = [
            "a:has-text('Next')",
            "a:has-text('>')",
            "input[value='Next']",
            "a[id*='Next']",
            "span[id*='Next'] a",
        ]
        for selector in next_selectors:
            el = page.locator(selector).first
            if await el.count() > 0:
                is_disabled = await el.get_attribute("disabled")
                css_class = (await el.get_attribute("class")) or ""
                if is_disabled or "disabled" in css_class.lower():
                    return False
                await el.click()
                await page.wait_for_load_state("load")
                return True

        return False

    def _extract_records(self, soup: BeautifulSoup) -> list[ScrapedRecord]:
        """Extract ScrapedRecord objects from the results table HTML.

        Uses heuristics to identify columns rather than fixed indices.
        """
        table = soup.find("table", id=re.compile(r"[Gg]rid|[Rr]esult|[Dd]ata"))
        if table is None:
            # Fallback: first table with > 2 columns and > 1 row
            for t in soup.find_all("table"):
                rows = t.find_all("tr")
                if len(rows) > 1:
                    cols = rows[0].find_all(["th", "td"])
                    if len(cols) >= 3:
                        table = t
                        break

        if table is None:
            _logger.warning("No results table found on page")
            return []

        rows = table.find_all("tr")
        if not rows:
            return []

        # Detect header row to understand column layout
        header_row = rows[0]
        header_cells = header_row.find_all(["th", "td"])
        headers = [self.clean(c.get_text()) or "" for c in header_cells]

        records: list[ScrapedRecord] = []

        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue

            cell_texts = [self.clean(c.get_text(separator=" ")) or "" for c in cells]

            if not any(cell_texts):
                continue

            record = self._map_row(cell_texts, headers)
            if record:
                records.append(record)

        return records

    def _map_row(self, cell_texts: list[str], headers: list[str]) -> ScrapedRecord | None:
        """Map a result row to a ScrapedRecord using header hints + heuristics."""
        record = ScrapedRecord()

        # ── Date: first cell matching date pattern ────────────────────────────
        for text in cell_texts:
            m = _DATE_RE.search(text)
            if m:
                record.date_recorded = m.group()
                break

        # ── Parcel ID: 10-digit number ────────────────────────────────────────
        for text in cell_texts:
            m = _PARCEL_RE.search(text)
            if m:
                record.parcel_id = m.group()
                break

        # ── Legal description: cell containing land survey keywords ───────────
        for text in cell_texts:
            if _LEGAL_KEYWORDS.search(text):
                record.legal_description = text
                break

        # ── Party name: longest text field not already used ───────────────────
        used = {record.date_recorded, record.parcel_id, record.legal_description}
        remaining = [t for t in cell_texts if t and t not in used]
        if remaining:
            record.party_name = max(remaining, key=len)

        # ── Heirs: look for header hint or secondary name fields ──────────────
        heir_idx = next(
            (i for i, h in enumerate(headers) if "heir" in h.lower() or "associated" in h.lower()),
            None,
        )
        if heir_idx is not None and heir_idx < len(cell_texts):
            record.heirs = cell_texts[heir_idx]

        # Skip rows that produced no meaningful data
        if not any([record.date_recorded, record.party_name, record.parcel_id]):
            return None

        return record
