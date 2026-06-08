"""Skagit County Recording Search template scraper.

Custom ASP.NET recorder portal at skagitcounty.net/Search/Recording/.
Not a shared platform (EagleWeb/AcclaimWeb/Tyler) — Skagit-specific but
could potentially serve as a base for other custom ASP.NET recorder sites.

Features:
- Date range search (content_txtStartDate, content_txtEndDate)
- Document type dropdown (content_ddlDocumentType) — 200+ types
- Results table with: File#/Date/DocType, Grantor, Grantee, Filer,
  Comment, Legal, Parcel/PLSS/Permit/TaxAcct
- Parcel IDs available directly in search results
- ASP.NET postback pagination (25 records/page)

Volume: ~486 records in 7 days (all types), ~130k population county.
"""

import re

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord, normalize_party_text
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.skagit")

_DOC_TYPE_MAP = {
    "probate": [
        "PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE", "DEATH CERTIFICATE", "AFFIDAVIT",
        "TRANSFER ON DEATH", "ESTATE", "WILL", "HEIR", "LACK OF PROBATE",
    ],
    "pre_foreclosure": [
        "LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
        "TRUSTEE'S SALE", "NOTICE OF DEFAULT", "FORECLOSURE",
    ],
    "tax_delinquent": [
        "TAX LIEN", "CERTIFICATE OF DELINQUENCY", "CERTIFICATE OF SALE",
        "FEDERAL TAX LIEN", "TREASURER",
    ],
    "divorce": [
        "DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION",
        "DECREE-DIVORCE", "SEPARATION",
    ],
}


class SkagitRecordingScraper(BridgeScraper):
    """Template scraper for Skagit County Recording Search.

    Custom ASP.NET portal — zero Claude AI cost, standardized selectors.
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
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        self.require_parcel_id = require_parcel_id

        from urllib.parse import urlparse
        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    # Map each record type to the exact dropdown option values in Skagit's
    # content_ddlDocumentType. Running one search per doc type keeps the
    # result set small enough to fit on page 1 (no pagination needed).
    _SERVER_DOC_TYPES: dict[str, list[str]] = {
        "probate": [
            "Affidavit", "Death Certificate", "Transfer on Death Deed", "Will",
        ],
        "pre_foreclosure": [
            "Lis Pendens", "Notice Of Default", "Notice Of Foreclosure",
            "Notice Of Trustees Sale",
        ],
        "tax_delinquent": [
            "Federal Tax Lien",
        ],
        "divorce": [
            "Decree-divorce",
        ],
    }

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        _logger.info(
            "Skagit Recording scraper - %s/%s - %s to %s",
            self.county, self.state, date_from, date_to,
        )

        # Determine which doc types to search for. Use server-side filtering
        # via the dropdown so each search returns a small result set that
        # fits on page 1. This avoids the ASP.NET pagination issue entirely.
        doc_types_to_search = []
        if self.active_record_type and self.active_record_type in self._SERVER_DOC_TYPES:
            doc_types_to_search = self._SERVER_DOC_TYPES[self.active_record_type]
        else:
            # No filter or unknown type — search all mapped types
            for types in self._SERVER_DOC_TYPES.values():
                doc_types_to_search.extend(types)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()

        for doc_type_label in doc_types_to_search:
            records = await self._search_one_doc_type(date_from, date_to, doc_type_label)
            new = 0
            for r in records:
                h = self.make_hash(r.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    r.raw_html_hash = h
                    all_records.append(r)
                    new += 1
            if new:
                _logger.info("Doc type '%s': %d new records", doc_type_label, new)

        _logger.info(
            "Skagit %s: %d total records from %d doc type searches",
            self.active_record_type or "all", len(all_records), len(doc_types_to_search),
        )

        if self.require_parcel_id:
            before = len(all_records)
            all_records = [r for r in all_records if r.parcel_id]
            dropped = before - len(all_records)
            if dropped:
                _logger.info("Dropped %d/%d records with no parcel_id", dropped, before)

        return all_records

    async def _search_one_doc_type(
        self, date_from: str, date_to: str, doc_type_label: str,
    ) -> list[ScrapedRecord]:
        """Run a single search for one doc type and extract all pages."""
        await self.navigate(self.base_url)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.page.wait_for_timeout(2_000)

        # Fill date range
        await self.page.locator("#content_txtStartDate").click()
        await self.page.locator("#content_txtStartDate").fill(date_from)
        await self.page.keyboard.press("Tab")
        await self.page.wait_for_timeout(500)
        await self.page.locator("#content_txtEndDate").fill(date_to)
        await self.page.keyboard.press("Tab")
        await self.page.wait_for_timeout(500)

        # Select doc type in dropdown
        try:
            await self.page.locator("#content_ddlDocumentType").select_option(label=doc_type_label)
        except Exception as exc:
            _logger.warning("Could not select doc type '%s': %s", doc_type_label, str(exc)[:80])
            return []

        # Force-hide calendar overlays
        await self.page.evaluate(
            "document.querySelectorAll('.ajax__calendar_container').forEach(c => c.style.display = 'none')"
        )

        # Click Search
        try:
            await self.page.locator(
                "input[type='submit'][value='Search'], input[name*='btnSearch']"
            ).first.click(timeout=10_000)
        except Exception as exc:
            _logger.warning("Search click failed for '%s': %s", doc_type_label, str(exc)[:80])
            return []

        await self.page.wait_for_timeout(5_000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Check result count
        body = await self.page.inner_text("body")
        count_match = re.search(r"returned\s+(\d+)\s+records?", body)
        total = int(count_match.group(1)) if count_match else 0
        if total == 0:
            return []

        # Extract all pages for this doc type
        all_page_records: list[ScrapedRecord] = []
        page_num = 1
        max_pages = 20

        while page_num <= max_pages:
            page_records = await self._extract_page()
            if not page_records:
                break
            all_page_records.extend(page_records)
            _logger.info(
                "  '%s' page %d: %d records (total %d/%d)",
                doc_type_label, page_num, len(page_records), len(all_page_records), total,
            )
            if len(all_page_records) >= total:
                break
            if not await self._goto_next_page():
                break
            page_num += 1

        return all_page_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the Skagit results table.

        Column layout (0-indexed, some cells span multiple sub-fields):
        [0-2]: empty/icons, [3]: FileNum+Date+DocType, [4]: Grantor,
        [5]: Grantee, [6]: Filer, [7]: Comment, [8]: Legal,
        [9]: Parcel+PLSS+Permit+TaxAcct
        """
        records: list[ScrapedRecord] = []
        try:
            raw = await self.page.evaluate(r"""() => {
                const tables = Array.from(document.querySelectorAll('table'));
                let best = null;
                for (const t of tables) {
                    const rows = Array.from(t.querySelectorAll('tr'));
                    if (rows.length < 3) continue;
                    // Find table with Grantor/Grantee headers
                    const text = t.textContent || '';
                    if (text.includes('Grantor') && text.includes('Grantee')) {
                        if (!best || rows.length > best.rows.length) {
                            best = { rows };
                        }
                    }
                }
                if (!best) return [];
                const out = [];
                // Skip header row
                for (let i = 1; i < best.rows.length; i++) {
                    const cells = best.rows[i].querySelectorAll('td');
                    if (cells.length < 8) continue;
                    // Cell 3 has combined: FileNumber\nDate\nDocType
                    const cell3 = (cells[3] || {}).textContent || '';
                    // Party cells: read innerHTML so structural separators
                    // (e.g. <br> between co-grantors) survive to Python's
                    // normalize_party_text(). textContent would collapse
                    // stacked parties in-browser before we can split them.
                    const grantor = (cells[4] || {}).innerHTML?.trim() || '';
                    const grantee = (cells[5] || {}).innerHTML?.trim() || '';
                    const comment = (cells[7] || {}).textContent?.trim() || '';
                    const legal = (cells[8] || {}).textContent?.trim() || '';
                    const parcelCell = (cells[9] || {}).textContent?.trim() || '';
                    out.push({cell3, grantor, grantee, comment, legal, parcelCell});
                }
                return out;
            }""")

            for item in raw:
                record = ScrapedRecord()
                # Parse cell3: "2026040100014/1/2026AFFIDAVIT"
                # Format: FileNum (pure digits) + Date (M/D/YYYY) + DocType
                # all concatenated with no separator. The file number never
                # contains "/" so we split on "/" to isolate the date.
                cell3 = item.get("cell3", "").strip()
                slashes = [i for i, c in enumerate(cell3) if c == "/"]
                if len(slashes) >= 2:
                    # First "/" separates month from day. Month is 1-2 chars
                    # before the first "/". Everything before month = file number.
                    first_slash = slashes[0]
                    # Month is at most 2 digits before the first "/"
                    month_start = first_slash - 2 if first_slash >= 2 and cell3[first_slash - 2].isdigit() else first_slash - 1
                    if month_start < 0:
                        month_start = 0
                    # Ensure we don't include non-month digits
                    month_str = cell3[month_start:first_slash]
                    if month_str.isdigit() and 1 <= int(month_str) <= 12:
                        pass  # good
                    else:
                        month_start = first_slash - 1
                    file_num = cell3[:month_start].strip()
                    # Second "/" separates day from year. Year is 4 digits after.
                    second_slash = slashes[1]
                    year_and_doc = cell3[second_slash + 1:]
                    year = year_and_doc[:4]
                    doc_type_text = year_and_doc[4:].strip()
                    day = cell3[first_slash + 1:second_slash]
                    month = cell3[month_start:first_slash]
                    record.date_recorded = f"{month}/{day}/{year}"
                    record.doc_type = doc_type_text if doc_type_text else None
                    if file_num:
                        record.enrichment_data["instrument_number"] = file_num

                # Grantor → party_name (normalize stacked parties -> " / ")
                grantor = normalize_party_text(item.get("grantor"))
                if grantor:
                    record.party_name = grantor

                # Grantee → heirs (normalize stacked parties -> " / ")
                grantee = normalize_party_text(item.get("grantee"))
                if grantee:
                    record.heirs = grantee

                # Comment — contains probate info like "INHERITANCE LACK OF PROBATE"
                # Use for doc_type filtering AND store in enrichment_data
                comment = item.get("comment", "").strip()
                if comment:
                    record.enrichment_data["comment"] = comment

                # Legal description
                legal = item.get("legal", "").strip()
                if legal:
                    record.legal_description = legal[:200]

                # Parcel — cell contains "P125914\n35040\n2-3-005-0100" etc
                # concatenated. The ACTUAL parcel is the "P" + digits portion
                # (e.g. "P125914") — the rest is PLSS/tax account data.
                # The WA statewide GIS matches on just the P-number.
                parcel_text = item.get("parcelCell", "")
                parcel_match = re.search(r"(P\d{4,})", parcel_text)
                if parcel_match:
                    record.parcel_id = parcel_match.group(1)
                elif not parcel_match:
                    # Fallback: any 6+ digit number
                    digit_match = re.search(r"\b(\d{6,})\b", parcel_text)
                    if digit_match:
                        record.parcel_id = digit_match.group(1)

                record.enrichment_data["source"] = "skagit_recording"

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))
        except Exception as exc:
            _logger.warning("Extraction error: %s", str(exc)[:120])

        return records

    async def _goto_next_page(self) -> bool:
        """Click next page in ASP.NET pagination."""
        try:
            # Skagit uses "Next" link for pagination
            next_link = self.page.locator(
                "a:has-text('Next'), a:has-text('next'), a:has-text('>')"
            ).first
            if await next_link.count() == 0:
                return False
            if not await next_link.is_visible():
                return False
            await next_link.click(timeout=10_000)
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
        """Filter by active record type using both doc_type AND comment fields."""
        if not self.active_record_type or self.active_record_type == "all":
            return records
        keywords = _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return records
        kept = []
        for r in records:
            # Check both doc_type and comment for keyword matches
            text = f"{r.doc_type or ''} {r.enrichment_data.get('comment', '')}".upper()
            if any(kw in text for kw in keywords):
                kept.append(r)
        return kept
