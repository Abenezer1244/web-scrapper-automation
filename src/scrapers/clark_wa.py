"""Clark County (WA) — LandmarkWeb scraper via Document Type checkbox search.

Portal: https://e-docs.clark.wa.gov/LandmarkWeb/
Platform: Hyland LandmarkWeb (different UI from King County)

Flow:
1. Navigate to home page → click "document" icon
2. Select doc type checkboxes (DEATH CERTIFICATE, NOTICE OF TRUSTEE SALE, etc.)
3. Set Document Category to "All Categories"
4. Fill Begin/End Date
5. Click Submit
6. Extract results from the table below (has PID in Legal column)

Clark uses checkbox multi-select for doc types, not a dropdown like King.
"""

import asyncio
import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.clark_wa")

_BASE_URL = "https://e-docs.clark.wa.gov/LandmarkWeb"
_PID_PATTERN = re.compile(r"PID[:\s]*(\d{6,12})", re.IGNORECASE)

# Map record_type → checkbox labels to select
_DOC_TYPES = {
    "probate": ["DEATH CERTIFICATE", "LACK OF PROBATE AFFIDAVIT", "TRANSFER ON DEATH DEED", "WILL"],
    "pre_foreclosure": ["NOTICE OF TRUSTEE SALE", "LIS PENDENS", "NOTICE OF DEFAULT",
                         "NOTICE OF FORECLOSURE", "FORECLOSURE", "TRUSTEES SALE"],
    "divorce": ["DISSOLUTION", "DIVORCE"],
    "tax_delinquent": ["CERTIFICATE OF DELIQUENCY", "CERTIFICATE OF SALE", "FEDERAL TAX LIEN"],
}

add_scrape_domain("e-docs.clark.wa.gov")


class ClarkWAScraper(BridgeScraper):
    """Clark County LandmarkWeb scraper — uses Document Type checkbox selection."""

    def __init__(self, record_type: str = "probate"):
        super().__init__()
        self._record_type = record_type
        self._doc_types = _DOC_TYPES.get(record_type, _DOC_TYPES["probate"])

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 30

        _logger.info("Clark WA %s — %s to %s (doc types: %s)",
                      self._record_type, date_from, date_to, self._doc_types)

        # Navigate and accept disclaimer
        await self.navigate(_BASE_URL)
        await self._accept_disclaimer()

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            try:
                records = await self._search_chunk(cf, ct)
            except Exception as exc:
                _logger.warning("Chunk failed: %s — skipping", str(exc)[:80])
                chunk_start = chunk_end
                continue

            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen:
                    seen.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)

            _logger.info("Chunk done: %d new (total %d)", len(records), len(all_records))

            if self.on_progress:
                self.on_progress(0, 0, len(all_records))

            chunk_start = chunk_end

        _logger.info("Clark WA %s complete — %d records", self._record_type, len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Accept disclaimer via SetDisclaimer() JS."""
        try:
            await self.page.wait_for_timeout(2000)
            has_fn = await self.page.evaluate("typeof SetDisclaimer === 'function'")
            if has_fn:
                await self.page.evaluate("SetDisclaimer()")
                await self.page.wait_for_timeout(3000)
                _logger.info("Disclaimer accepted via JS")
            else:
                _logger.info("No SetDisclaimer function — continuing")
        except Exception as exc:
            _logger.info("Disclaimer: %s", str(exc)[:80])

    async def _search_chunk(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Navigate to Document Type search, select types, fill dates, submit, extract."""

        # Click "document" icon on home page
        doc_icon = self.page.locator("a:has-text('document'), img[alt*='document' i]").first
        try:
            await doc_icon.click()
            await self.page.wait_for_timeout(2000)
        except Exception:
            # Fallback: click Document Type tab
            tab = self.page.locator("#searchCriteriaDocuments-tab, a:has-text('Document Type')")
            if await tab.count() > 0:
                await tab.first.click()
                await self.page.wait_for_timeout(2000)

        # Select "Custom Selection" from category dropdown
        await self.page.evaluate("""() => {
            const sel = document.querySelector('#documentCategory-DocumentType');
            if (!sel) return;
            // Find "Custom Selection" option
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().includes('custom')) {
                    sel.value = opt.value;
                    if (window.jQuery) jQuery('#documentCategory-DocumentType').val(opt.value).trigger('change');
                    break;
                }
            }
        }""")
        await self.page.wait_for_timeout(1500)

        # Fill the Custom Selection textarea with doc types (one per line)
        doc_types_text = "\n".join(self._doc_types)
        filled = await self.page.evaluate("""(text) => {
            const ta = document.querySelector('#documentType-DocumentType, textarea[name="documentType"], #documentType-Name');
            if (!ta) return false;
            ta.value = text;
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""", doc_types_text)
        _logger.info("Filled Custom Selection textarea with %d doc types: %s", len(self._doc_types), filled)

        # Fill dates
        begin = self.page.locator("#beginDate-DocumentType")
        end_el = self.page.locator("#endDate-DocumentType")
        if await begin.count() > 0:
            await begin.click()
            await begin.fill("")
            await begin.press_sequentially(date_from, delay=30)
            await end_el.click()
            await end_el.fill("")
            await end_el.press_sequentially(date_to, delay=30)
            _logger.info("Dates: %s to %s", date_from, date_to)

        # Submit
        await self.page.evaluate("""() => {
            const btn = document.querySelector('#submit-DocumentType');
            if (btn) btn.click();
        }""")

        # Wait for results
        try:
            await self.page.wait_for_function(
                """() => {
                    const sr = document.querySelector('#searchResults');
                    if (!sr) return false;
                    const html = sr.innerHTML;
                    if (html.includes('ajax-loader') || html.includes('LOADING')) return false;
                    return html.includes('<table') || html.toLowerCase().includes('no results');
                }""",
                timeout=60_000,
            )
        except Exception:
            await self.page.wait_for_timeout(15_000)

        await self.page.wait_for_timeout(5000)
        return await self._extract_results()

    async def _extract_results(self) -> list[ScrapedRecord]:
        """Extract records from the results table."""
        records: list[ScrapedRecord] = []

        raw = await self.page.evaluate("""() => {
            // Find the table with most rows
            const allTables = document.querySelectorAll('table');
            let best = null, bestRows = 0;
            for (const t of allTables) {
                const rows = t.querySelectorAll('tbody tr').length;
                if (rows > bestRows) { bestRows = rows; best = t; }
            }
            if (!best || bestRows < 1) return [];

            const rows = best.querySelectorAll('tbody tr');
            const results = [];
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 8) continue;

                // Scan all cells for data
                let grantor = '', grantee = '', dateStr = '', docType = '', legal = '', recNum = '';
                for (let i = 0; i < cells.length; i++) {
                    const t = cells[i].textContent.trim();
                    // Date pattern
                    if (!dateStr && /\\d{2}\\/\\d{2}\\/\\d{4}/.test(t)) dateStr = t;
                    // PID pattern
                    if (t.includes('PID') || t.includes('SUB:') || t.includes('LOT')) {
                        if (!legal || t.includes('PID')) legal = t;
                    }
                }
                // Known positions (from Clark's structure)
                if (cells[5]) grantor = cells[5].textContent.trim();
                if (cells[6]) grantee = cells[6].textContent.trim();
                if (cells[7]) dateStr = dateStr || cells[7].textContent.trim();
                if (cells[8]) docType = cells[8].textContent.trim();
                if (cells[12]) recNum = cells[12].textContent.trim();

                if (grantor || dateStr) {
                    results.push({grantor, grantee, date: dateStr, docType, legal, recNum});
                }
            }
            return results;
        }""")

        if not raw:
            _logger.info("No results found")
            return []

        _logger.info("Extracted %d rows", len(raw))

        for item in raw:
            legal = (item.get("legal") or "").strip()
            pid_match = _PID_PATTERN.search(legal)
            if not pid_match:
                continue

            record = ScrapedRecord()
            record.parcel_id = pid_match.group(1)
            record.party_name = (item.get("grantor") or "").strip()
            record.heirs = (item.get("grantee") or "").strip()
            record.doc_type = (item.get("docType") or "").strip()
            record.legal_description = (item.get("recNum") or "").strip()

            date_str = (item.get("date") or "").strip()
            date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
            if date_match:
                record.date_recorded = date_match.group(1)

            record.enrichment_data = {
                "source": "clark_county_recorder",
                "recording_number": item.get("recNum"),
                "parcel_id": record.parcel_id,
                "legal_description": legal,
                "doc_type": record.doc_type,
            }

            if record.party_name or record.date_recorded:
                records.append(record)

        _logger.info("Records with PID: %d / %d", len(records), len(raw))
        return records
