"""Laserfiche WebLink template scraper for county recorder portals.

Covers counties using Laserfiche WebLink 10 for public record search.
First confirmed: Cowlitz County, WA.

Laserfiche WebLink sites share:
- Welcome page at /WLAudPublic/welcome.aspx (no disclaimer, just entry links)
- Custom search form at /CustomSearch.aspx with numbered inputs:
  - Search_Input0 + Search_Input0_end: recording date range
  - Search_Input11: document type filter (optional)
  - Submit button triggers ASP.NET postback
- Results table with columns:
  AFN, Recording Date, Doc Type Desc, Volume, Page, Grantor, Grantee, Parcel
- Parcel IDs are available IN the search results (unlike Tyler SelfService)
- Pagination via ASP.NET __doPostBack pager links

Volume reference (Cowlitz 2026-04-12):
- 7-day total: 475 records (all types)
"""

import re

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.laserfiche")

_DOC_TYPE_MAP = {
    "probate": [
        "PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE", "PERSONAL REP", "DEATH CERTIFICATE",
        "AFFIDAVIT OF HEIRSHIP", "TRANSFER ON DEATH", "ESTATE", "WILL",
        "HEIR",
    ],
    "pre_foreclosure": [
        "LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
        "TRUSTEE'S SALE", "NOTICE OF DEFAULT", "FORECLOSURE",
    ],
    "tax_delinquent": [
        "TAX LIEN", "CERTIFICATE OF DELINQUENCY", "TAX DELINQUENT",
        "CERTIFICATE OF SALE", "TREASURER",
    ],
    "divorce": [
        "DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION",
    ],
}


class LaserficheWebLinkScraper(BridgeScraper):
    """Template scraper for Laserfiche WebLink recorder portals.

    Zero Claude AI cost — uses standardized ASP.NET form selectors.
    Parcel IDs are available directly in search results.
    """

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        require_parcel_id: bool = True,
    ):
        super().__init__()
        # Normalise base_url to the WLAudPublic root
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        # Extract the app path (e.g. /WLAudPublic or /WLAUDPublic)
        path_parts = parsed.path.strip("/").split("/")
        app_path = path_parts[0] if path_parts else "WLAudPublic"
        self.app_root = f"{scheme}://{host}/{app_path}"
        self.base_url = base_url
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        self.require_parcel_id = require_parcel_id

        if host:
            add_scrape_domain(host)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from a Laserfiche WebLink portal."""
        from datetime import datetime, timedelta

        _logger.info(
            "Laserfiche WebLink scraper - %s/%s - %s to %s",
            self.county, self.state, date_from, date_to,
        )

        # Navigate to the search page
        search_url = f"{self.app_root}/CustomSearch.aspx?SearchName=Search&dbid=0&repo=CCIMAGES"
        await self.navigate(search_url)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.page.wait_for_timeout(2_000)

        # Fill date range
        _logger.info("Filling dates: %s to %s", date_from, date_to)
        try:
            await self.page.locator("#Search_Input0").fill(date_from)
            await self.page.locator("#Search_Input0_end").fill(date_to)
        except Exception as exc:
            _logger.error("Failed to fill dates: %s", str(exc)[:120])
            return []

        # Submit search
        try:
            await self.page.locator("input[type='submit'][value='Submit']").click()
            await self.page.wait_for_timeout(5_000)
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as exc:
            _logger.error("Submit failed: %s", str(exc)[:120])
            return []

        # Check for result count
        body = await self.page.inner_text("body")
        count_match = re.search(r"(\d+)\s+Results?", body)
        total = int(count_match.group(1)) if count_match else 0
        _logger.info("Search returned %d total results", total)
        if total == 0:
            return []

        # Extract all pages
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 1
        max_pages = 50

        while page_num <= max_pages:
            page_records = await self._extract_page()
            new = 0
            for r in page_records:
                h = self.make_hash(r.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    r.raw_html_hash = h
                    all_records.append(r)
                    new += 1
            _logger.info("Page %d: %d new records (total: %d)", page_num, new, len(all_records))

            if new == 0:
                break
            if not await self._goto_next_page():
                break
            page_num += 1

        # Filter by active record type
        filtered = self._filter_by_type(all_records)
        _logger.info(
            "Laserfiche %s: %d/%d after %s filter",
            self.county, len(filtered), len(all_records), self.active_record_type or "none",
        )

        # Drop records without parcel_id if required
        if self.require_parcel_id:
            before = len(filtered)
            filtered = [r for r in filtered if r.parcel_id]
            dropped = before - len(filtered)
            if dropped:
                _logger.info("Dropped %d/%d records with no parcel_id", dropped, before)

        return filtered

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current results table.

        Laserfiche WebLink renders results in a standard HTML table.
        Column order (Cowlitz verified):
        0=checkbox, 1=AFN, 2=Recording Date, 3=Doc Type, 4=Volume,
        5=Page, 6=Grantor, 7=Grantee, 8=Parcel
        """
        records: list[ScrapedRecord] = []
        try:
            raw = await self.page.evaluate("""() => {
                // Find the results table — pick the table with the most
                // rows where at least one row has a date (M/D/YYYY) in
                // the 3rd cell. Laserfiche WebLink doesn't always include
                // recognisable header text in the results table itself.
                const tables = Array.from(document.querySelectorAll('table'));
                let best = null;
                for (const t of tables) {
                    const rows = Array.from(t.querySelectorAll('tr'));
                    if (rows.length < 3) continue;
                    // Check if any row has a date pattern in cell[2]
                    let hasDate = false;
                    for (let i = 0; i < Math.min(rows.length, 5); i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length >= 6) {
                            const val = (cells[2]?.textContent || '').trim();
                            if (/\d{1,2}\/\d{1,2}\/\d{4}/.test(val)) { hasDate = true; break; }
                        }
                    }
                    if (hasDate && (!best || rows.length > best.rows.length)) {
                        best = { table: t, rows: rows };
                    }
                }
                if (!best) return [];

                const out = [];
                // Skip header row(s) — find first row with a date pattern
                for (const row of best.rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 6) continue;
                    // Extract text from each cell
                    const vals = Array.from(cells).map(c => (c.textContent || '').trim());
                    // Look for a date in position 2 (MM/DD/YYYY or M/D/YYYY)
                    const dateVal = vals[2] || '';
                    if (!/\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(dateVal)) continue;
                    out.push({
                        afn: vals[1] || '',
                        date: dateVal,
                        doc_type: vals[3] || '',
                        volume: vals[4] || '',
                        page_num: vals[5] || '',
                        grantor: vals[6] || '',
                        grantee: vals[7] || '',
                        parcel: vals[8] || '',
                    });
                }
                return out;
            }""")

            for item in raw:
                record = ScrapedRecord()
                # Date
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", item.get("date", ""))
                if date_match:
                    record.date_recorded = date_match.group(1)
                # Doc type
                doc_type = item.get("doc_type", "").strip()
                record.doc_type = doc_type if doc_type else None
                # Grantor → party_name
                grantor = item.get("grantor", "").strip()
                if grantor:
                    record.party_name = grantor
                # Grantee → heirs
                grantee = item.get("grantee", "").strip()
                if grantee:
                    record.heirs = grantee
                # Parcel
                parcel = item.get("parcel", "").strip()
                if parcel and re.match(r"[\d\w]", parcel):
                    record.parcel_id = parcel
                # Instrument number
                afn = item.get("afn", "").strip()
                record.enrichment_data = {
                    "instrument_number": afn,
                    "source": "laserfiche_weblink",
                }
                # Legal description from volume/page
                vol = item.get("volume", "").strip()
                pg = item.get("page_num", "").strip()
                if vol or pg:
                    record.legal_description = f"Vol {vol} Pg {pg}".strip()

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))
        except Exception as exc:
            _logger.warning("Extraction error: %s", str(exc)[:120])

        return records

    async def _goto_next_page(self) -> bool:
        """Click the next page link in Laserfiche WebLink pagination.

        WebLink uses ASP.NET __doPostBack for pagination. The pager
        typically has numbered links and a 'Next' or '>' link.
        """
        try:
            # Laserfiche WebLink pager uses "Next" link (exact text match).
            # Use text= selector for exact match to avoid matching numbered
            # page links that also contain digits.
            next_link = self.page.get_by_text("Next", exact=True).first
            if await next_link.count() == 0:
                # Try ">" as fallback
                next_link = self.page.get_by_text(">", exact=True).first
                if await next_link.count() == 0:
                    return False
            # Laserfiche pager links may be outside the viewport or
            # styled as invisible by CSS — use force=True to bypass
            # Playwright's visibility check.
            await next_link.click(timeout=5_000, force=True)
            await self.page.wait_for_timeout(3_000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return True
        except Exception as exc:
            _logger.info("Pagination ended: %s", str(exc)[:80])
            return False

    def _filter_by_type(self, records: list[ScrapedRecord]) -> list[ScrapedRecord]:
        """Keep only records whose doc_type matches the active record type."""
        if not self.active_record_type or self.active_record_type == "all":
            return records
        keywords = _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return records
        kept = []
        for r in records:
            doc = (r.doc_type or "").upper()
            if any(kw in doc for kw in keywords):
                kept.append(r)
        return kept
