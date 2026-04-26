import asyncio
"""AVA Fidlar template scraper for Fidlar Technologies AVA recorder portals.

Covers WA counties using the AVA (Angular SPA) interface.
No Claude AI needed — standardized Angular Material navigation + extraction.

AVA sites share:
- Angular SPA with hash routing (#/search)
- Material Design date inputs (mat-input-0, mat-input-1)
- Document Type autocomplete (mat-input-2)
- Search/Reset buttons
- Results rendered as Material card components
- reCAPTCHA present but not enforced for basic searches

Counties using AVA in WA:
Yakima (ava.fidlar.com/WAYakima/AvaWeb)
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.ava_fidlar")

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


class AvaFidlarScraper(BridgeScraper):
    """Template scraper for Fidlar AVA Angular SPA recorder sites.

    Zero Claude AI cost — uses standardized Angular Material selectors.
    Must wait for Angular to bootstrap before interacting.
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
        """Scrape records from an AVA Fidlar site using date chunking."""
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7

        _logger.info(
            "AVA Fidlar scraper — %s/%s — %s to %s (%d-day chunks)",
            self.county, self.state, date_from, date_to, chunk_days,
        )

        # Navigate and wait for Angular to bootstrap
        await self.navigate(self.base_url)
        await self._wait_for_angular()

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            # Fill dates and search
            await self._fill_dates(cf, ct)
            await self._submit_search()

            # Extract results
            chunk_records = await self._extract_results()

            # Go back to search for next chunk
            if chunk_end < end:
                await self._go_back_to_search()

            new_count = self.dedupe_extend(chunk_records, seen_hashes, all_records)

            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new_count, len(all_records))

            chunk_start = chunk_end
            pass

        # Enrich records with parcel data
        _logger.info("ava_fidlar complete - %d records (enrichment runs after save)", len(all_records))
        return all_records

    async def _wait_for_angular(self) -> None:
        """Wait for Angular SPA to fully bootstrap."""
        try:
            # Wait for app-root to have content (Angular renders into <app-root>)
            await self.page.wait_for_selector(
                "app-root input, app-root button, mat-form-field",
                timeout=15_000,
            )
            await self.page.wait_for_timeout(2_000)
            _logger.info("Angular app loaded, URL: %s", self.page.url)
        except Exception as exc:
            _logger.warning("Angular load timeout: %s", str(exc)[:80])
            await self.page.wait_for_timeout(5_000)

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the Angular Material date inputs."""
        try:
            # Date inputs are mat-input-0 (from) and mat-input-1 (to)
            from_input = self.page.locator("#mat-input-0")
            to_input = self.page.locator("#mat-input-1")

            if await from_input.count() == 0:
                # Fallback: find by placeholder
                from_input = self.page.locator("input[placeholder='MM/DD/YYYY']").first
                to_input = self.page.locator("input[placeholder='MM/DD/YYYY']").nth(1)

            # Clear and fill from date
            await from_input.click()
            await from_input.fill("")
            await from_input.fill(date_from)
            await from_input.press("Tab")

            # Clear and fill to date
            await to_input.click()
            await to_input.fill("")
            await to_input.fill(date_to)
            await to_input.press("Tab")

            await self.page.wait_for_timeout(500)
            _logger.info("Dates filled: %s to %s", date_from, date_to)

        except Exception as exc:
            _logger.warning("Could not fill dates: %s", str(exc)[:120])

    async def _submit_search(self) -> None:
        """Click the Search button and wait for results."""
        try:
            search_btn = self.page.locator("button:has-text('Search')").first
            await search_btn.click()
            _logger.info("Search clicked, waiting for results...")

            # Wait for results to appear or "no results" message
            await self.page.wait_for_timeout(8_000)

            body = await self.page.inner_text("body")
            results_match = re.search(r"Results:\s*(\d+)", body)
            if results_match:
                _logger.info("Results count: %s", results_match.group(1))
            elif "no results" in body.lower():
                _logger.info("No results for this date range")

        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:120])

    async def _extract_results(self) -> list[ScrapedRecord]:
        """Extract records from the AVA results page.

        AVA renders results as Angular Material card-like components.
        Each record shows: Document No, Document Type, Recorded Date, Party1, Party2, Legals
        Expanded details show: Ref No, Page Count, full party names, legal descriptions.
        """
        records: list[ScrapedRecord] = []

        try:
            raw = await self.page.evaluate("""
                (() => {
                    const body = document.body.innerText;
                    const results = [];

                    // Parse the text-based card layout
                    // Pattern: Document No, Type, Date on header line
                    // Then Party1, Party2, Legals
                    // Then expanded details with more info

                    // Split by document number pattern (6+ digit number at start)
                    const sections = body.split(/(?=\\b\\d{9,}\\b)/);

                    for (const section of sections) {
                        const lines = section.trim().split('\\n').map(l => l.trim()).filter(l => l);
                        if (lines.length < 3) continue;

                        const record = {};

                        // First line: document number
                        const docMatch = lines[0].match(/^(\\d{9,})/);
                        if (!docMatch) continue;
                        record.instrument = docMatch[1];

                        // Find document type
                        for (const line of lines.slice(0, 5)) {
                            if (/^(DEED|MORTGAGE|LIEN|RELEASE|ASSIGNMENT|PROBATE|WILL|DEATH|ESTATE|TRUST|DIVORCE|DISSOLUTION|DEFAULT|FORECLOSURE|NOTICE|AFFIDAVIT|TAX|SURVEY|PLAT|EASEMENT|POWER|SATISFACTION|MARRIAGE|MISC|UCC|JUDGM|DECREE|CERTIFICATE)/i.test(line)) {
                                record.doc_type = line;
                                break;
                            }
                        }

                        // Find date
                        const dateMatch = section.match(/(\\d{1,2}\\/\\d{1,2}\\/\\d{4})\\s+\\d{1,2}:\\d{2}/);
                        if (dateMatch) record.date_recorded = dateMatch[1];

                        // Find parties
                        const party1Match = section.match(/Party\\s*1:\\s*([^\\n]+)/);
                        if (party1Match) record.grantor = party1Match[1].trim();
                        const party2Match = section.match(/Party\\s*2:\\s*([^\\n]+)/);
                        if (party2Match) record.grantee = party2Match[1].trim();

                        // Fallback: lines after doc type often have party names
                        if (!record.grantor) {
                            for (let i = 2; i < Math.min(lines.length, 6); i++) {
                                if (/^[A-Z][A-Z ]+$/.test(lines[i]) && lines[i].length > 3) {
                                    if (!record.grantor) record.grantor = lines[i];
                                    else if (!record.grantee) { record.grantee = lines[i]; break; }
                                }
                            }
                        }

                        // Find legal/parcel
                        const parcelMatch = section.match(/(\\d{6}-\\d{5})/);
                        if (parcelMatch) record.parcel = parcelMatch[1];

                        // Legal description from Legals section
                        const legalMatch = section.match(/Legals\\s*([^\\n]+(?:\\n[^P][^a].*)*)/);
                        if (legalMatch) record.legal = legalMatch[1].trim().split('\\n')[0];

                        if (record.instrument && (record.grantor || record.date_recorded)) {
                            results.push(record);
                        }
                    }

                    // Deduplicate by instrument number
                    const seen = new Set();
                    return results.filter(r => {
                        if (seen.has(r.instrument)) return false;
                        seen.add(r.instrument);
                        return true;
                    });
                })()
            """)

            if not raw:
                _logger.info("No records extracted from results page")
                return []

            for item in raw:
                record = ScrapedRecord()

                inst = (item.get("instrument") or "").strip()
                if inst:
                    record.legal_description = inst

                date_str = (item.get("date_recorded") or "").strip()
                if date_str:
                    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                    if date_match:
                        record.date_recorded = date_match.group(1)

                doc_type = (item.get("doc_type") or "").strip().upper()

                # Filter by record type
                if self.record_types and doc_type:
                    matched = False
                    for rt in self.record_types:
                        keywords = _DOC_TYPE_MAP.get(rt, [])
                        if any(kw in doc_type for kw in keywords):
                            matched = True
                            break
                    if not matched:
                        continue

                grantor = (item.get("grantor") or "").strip()
                if grantor:
                    record.party_name = grantor

                grantee = (item.get("grantee") or "").strip()
                if grantee:
                    record.heirs = grantee

                legal = (item.get("legal") or "").strip()
                if legal and record.legal_description:
                    record.legal_description = f"{record.legal_description} | {legal}"
                elif legal:
                    record.legal_description = legal

                parcel = (item.get("parcel") or "").strip()
                if parcel:
                    record.parcel_id = parcel

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from results", len(records))

        except Exception as exc:
            _logger.warning("Error extracting results: %s", str(exc)[:80])

        return records

    async def _go_back_to_search(self) -> None:
        """Navigate back to the search form for the next chunk."""
        try:
            back_btn = self.page.locator("button:has-text('Back'), a:has-text('Back')")
            if await back_btn.count() > 0:
                await back_btn.first.click()
                await self.page.wait_for_timeout(2_000)
                _logger.info("Navigated back to search")
                return
        except Exception:
            pass

        # Fallback: navigate directly
        await self.page.goto(f"{self.base_url}/#/search", wait_until="networkidle", timeout=15_000)
        await self._wait_for_angular()
