"""King County (WA) Superior Court Clerk — Probate/Guardianship scraper.

Portal: https://dja-prd-ecexap1.kingcounty.gov/node/411?caseType=511110
Platform: Journal Technologies eCourt
No CAPTCHA required.

Search by Filing Date range, extracts:
- Case Number (e.g. 26-4-02709-6 SEA)
- Filing Date
- Case Name / Party Name
- Charge/Cause of Action:
    Estate, Non Probate Notice to Creditor, Guardianship / Conservatorship,
    Minor Settlement, Trust, Trust/Estate Dispute Resolution, Will Only,
    Non Judicial Binding/TEDRA Agreement, Miscellaneous,
    Emergency Minor Guardianship
- Next Hearing date
- Status (Active / Completed)
- Court Location (SEA = Seattle, KNT = Kent)
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_wa_probate")

_BASE_URL = "https://dja-prd-ecexap1.kingcounty.gov"
_SEARCH_URL = f"{_BASE_URL}/node/411?caseType=511110"

# Field IDs on the Journal Technologies form
_FROM_DATE_ID = "#dataRange_from_324159715051700"
_TO_DATE_ID = "#dataRange_to_324159715051700"
_SUBMIT_ID = "#edit-submit"
_TABLE_SEL = 'table[id*="searchPage"]'


class KingWaProbateScraper(BridgeScraper):
    """Scrapes probate/guardianship filings from King County Superior Court."""

    def __init__(self):
        super().__init__()
        add_scrape_domain("dja-prd-ecexap1.kingcounty.gov")

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7

        _logger.info(
            "King County probate — %s to %s (%d-day chunks)",
            date_from, date_to, chunk_days,
        )

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            records = await self._search_chunk(cf, ct)

            new_count = 0
            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen:
                    seen.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info(
                "Chunk %s–%s: %d new (total %d)",
                cf, ct, new_count, len(all_records),
            )

            if self.on_progress:
                self.on_progress(None, None, len(all_records))

            chunk_start = chunk_end

        _logger.info("King County probate complete — %d records", len(all_records))
        return all_records

    async def _search_chunk(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Navigate to search page, fill dates, submit, extract all pages."""
        await self.navigate(_SEARCH_URL)
        await self.page.wait_for_timeout(4000)

        # Fill Filing Date range
        from_el = self.page.locator(_FROM_DATE_ID)
        to_el = self.page.locator(_TO_DATE_ID)

        await from_el.click()
        await from_el.fill(date_from)
        await from_el.press("Tab")
        await self.page.wait_for_timeout(200)

        await to_el.click()
        await to_el.fill(date_to)
        await to_el.press("Tab")
        await self.page.wait_for_timeout(300)

        # Submit search
        await self.page.locator(_SUBMIT_ID).click()
        _logger.info("Search submitted")

        # Wait for results table
        try:
            await self.page.wait_for_selector(
                f'{_TABLE_SEL} tbody tr',
                timeout=30_000,
            )
        except Exception:
            await self.page.wait_for_timeout(8000)

        return await self._extract_all_pages()

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages."""
        all_records: list[ScrapedRecord] = []
        seen_cases: set[str] = set()
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1
            records = await self._extract_page()

            # Deduplicate by case number within this chunk
            new_records = []
            for r in records:
                case_num = (r.enrichment_data or {}).get("case_number", "")
                if case_num and case_num not in seen_cases:
                    seen_cases.add(case_num)
                    new_records.append(r)

            all_records.extend(new_records)
            _logger.info("Page %d — %d new records (total %d)", page_num, len(new_records), len(all_records))

            # If no new records, we've looped back to the start
            if not new_records:
                _logger.info("No new records — pagination complete")
                break

            has_next = await self._go_next_page()
            if not has_next:
                break

        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current results page."""
        records: list[ScrapedRecord] = []

        try:
            raw = await self.page.evaluate("""
                (() => {
                    const table = document.querySelector('table[id*="searchPage"]');
                    if (!table) return [];

                    const rows = table.querySelectorAll('tbody tr');
                    return Array.from(rows).map(row => {
                        const cells = Array.from(row.querySelectorAll('td'));
                        if (cells.length < 4) return null;

                        const caseNum = (cells[0]?.textContent || '').trim();
                        // Skip spacer rows (empty or no case number pattern)
                        if (!caseNum || !/\\d{2}-\\d-\\d{5}/.test(caseNum)) return null;

                        return {
                            case_number: caseNum,
                            filing_date: (cells[1]?.textContent || '').trim(),
                            case_name: (cells[2]?.textContent || '').trim(),
                            cause: (cells[3]?.textContent || '').trim(),
                            next_hearing: (cells[4]?.textContent || '').trim(),
                            status: (cells[5]?.textContent || '').trim(),
                        };
                    }).filter(r => r !== null);
                })()
            """)

            if not raw:
                body_text = await self.page.inner_text("body")
                if "no results" in body_text.lower() or "0 record" in body_text.lower():
                    _logger.info("No results found")
                return []

            for item in raw:
                record = ScrapedRecord()

                # Full case name preserved
                case_name = item.get("case_name", "").strip()

                # Extract party name (e.g. "IN RE JOHN DOE" -> "JOHN DOE")
                name = re.sub(r"^IN\s+RE\s+(?:THE\s+)?(?:ESTATE\s+OF\s+)?", "", case_name, flags=re.IGNORECASE).strip()
                if name:
                    record.party_name = name

                # Filing date
                date_str = item.get("filing_date", "")
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                if date_match:
                    record.date_recorded = date_match.group(1)

                # Cause of action as doc_type
                cause = item.get("cause", "").strip()
                if cause:
                    record.doc_type = cause

                # Case number
                case_num = item.get("case_number", "").strip()
                if case_num:
                    record.legal_description = case_num

                # Parse court location from case number (SEA/KNT suffix)
                court_match = re.search(r"\b(SEA|KNT)\b", case_num)
                court_location = court_match.group(1) if court_match else ""

                # Next hearing (e.g. "Probate/Guardianship 05/21/2026")
                next_hearing = item.get("next_hearing", "").strip()

                # Status (e.g. "Active 03/27/2026", "Completed 03/27/2026")
                status_raw = item.get("status", "").strip()
                status = status_raw.split()[0] if status_raw else ""

                # Store all King County-specific fields in enrichment_data
                record.enrichment_data = {
                    "source": "king_county_court",
                    "case_number": case_num,
                    "case_name": case_name,
                    "cause_of_action": cause,
                    "next_hearing": next_hearing,
                    "status": status,
                    "court_location": court_location,
                }

                if record.party_name or record.date_recorded:
                    records.append(record)

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:120])

        return records

    async def _go_next_page(self) -> bool:
        """Click the next page link if it exists.

        Journal Technologies uses a single forward-arrow link in .pagination.
        When there are no more pages, the .pagination element is empty or absent.
        Clicking (not navigating) preserves the search session state.
        """
        try:
            next_link = self.page.locator('.pagination a')
            if await next_link.count() > 0:
                await next_link.first.click()
                await self.page.wait_for_timeout(5000)
                return True
        except Exception:
            pass
        return False
