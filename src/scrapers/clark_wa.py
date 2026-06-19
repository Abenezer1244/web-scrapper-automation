"""Clark County (WA) — LandmarkWeb scraper via Document Type modal checkbox search.

Portal: https://e-docs.clark.wa.gov/LandmarkWeb/
Platform: Hyland LandmarkWeb (different UI from King County)

Flow:
1. Navigate to home page → accept disclaimer via SetDisclaimer() JS
2. Click Document Type tab
3. Open doc-type modal via ShowDocumentModal()
4. Check checkboxes for target doc types (e.g., DEATH CERTIFICATE, WILL)
5. Click "Select" button in modal to apply
6. Fill Begin/End Date
7. Click Submit
8. Extract results from the results table (PID in Legal column)

Clark's doc-type modal uses checkbox IDs like `dt-DocumentType-{value}`.
The portal does NOT record divorce/dissolution — those are court records.
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord, normalize_party_text
from src.scrapers.preforeclosure import (
    is_cancellation_or_admin,
    orient_pre_foreclosure_party,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.clark_wa")

_BASE_URL = "https://e-docs.clark.wa.gov/LandmarkWeb"
_PID_PATTERN = re.compile(r"PID[:\s]*(\d{6,12})", re.IGNORECASE)
# Fallback: Clark's legal column may print the parcel without the "PID:" prefix.
# Accept any 6-12 digit run that appears alongside Lot/Block/Sub/Plat tokens.
_PID_FALLBACK_PATTERN = re.compile(
    r"(?:LOT|BLOCK|SUB|PLAT|TRACT|PARCEL|APN)[^\n]{0,80}?\b(\d{6,12})\b",
    re.IGNORECASE,
)

# Map record_type → checkbox labels to select
_DOC_TYPES = {
    "probate": ["DEATH CERTIFICATE", "LACK OF PROBATE AFFIDAVIT", "TRANSFER ON DEATH DEED", "WILL"],
    "pre_foreclosure": ["NOTICE OF TRUSTEE SALE", "LIS PENDENS", "NOTICE OF DEFAULT",
                         "NOTICE OF FORECLOSURE", "FORECLOSURE", "TRUSTEES SALE"],
    "divorce": ["DISSOLUTION", "DIVORCE"],
    "tax_delinquent": ["CERTIFICATE OF DELIQUENCY", "CERTIFICATE OF SALE", "FEDERAL TAX LIEN"],
}

# Map record_type → checkbox value IDs on the Document Type tab.
# These are the `value` attributes of `input[type="checkbox"]` elements
# with IDs like `dt-DocumentType-{value}`.
# Discovered from the live portal on 2026-04-18.
_DOC_TYPE_CHECKBOX_VALUES = {
    "probate": ["62", "316", "340", "278"],       # DEATH CERT, LACK OF PROBATE, TOD DEED, WILL
    "pre_foreclosure": ["167", "129", "166", "157", "93"],  # TRUSTEE SALE, LIS PENDENS, DEFAULT, FORECL
    "divorce": ["68", "71"],                        # DISSOLUTION, DIVORCE
    "tax_delinquent": ["97"],                       # FEDERAL TAX LIEN
}

# Record types where records don't have parcel IDs (e.g., divorce, probate)
_NO_PID_RECORD_TYPES = {"divorce"}

add_scrape_domain("e-docs.clark.wa.gov")


class ClarkWAScraper(BridgeScraper):
    """Clark County LandmarkWeb scraper — uses Document Type checkbox selection."""

    def __init__(self, record_type: str = "probate"):
        super().__init__()
        self._record_type = record_type
        self._doc_types = _DOC_TYPES.get(record_type, _DOC_TYPES["probate"])
        self._checkbox_values = _DOC_TYPE_CHECKBOX_VALUES.get(record_type, [])
        self._skip_pid = record_type in _NO_PID_RECORD_TYPES

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
                records = await self._search_chunk_all_pages(cf, ct)
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
        """Navigate to Document Type search, select doc types via modal, fill dates, submit."""

        # Click Document Type tab
        tab = self.page.locator("#searchCriteriaDocuments-tab")
        if await tab.count() > 0:
            await tab.click()
            await self.page.wait_for_timeout(2000)
        else:
            doc_icon = self.page.locator("a:has-text('document'), img[alt*='document' i]").first
            await doc_icon.click()
            await self.page.wait_for_timeout(2000)

        # Open the document type modal via the "select" button
        await self.page.evaluate(
            "ShowDocumentModal($('#documentTypeModal-DocumentType'))"
        )
        await self.page.wait_for_timeout(1500)

        # Check the doc type checkboxes inside the modal
        for val in self._checkbox_values:
            cb = self.page.locator(f"#dt-DocumentType-{val}")
            try:
                await cb.check(timeout=5000)
            except Exception:
                # Fallback: force via JS
                await self.page.evaluate(f"""() => {{
                    const cb = document.querySelector('#dt-DocumentType-{val}');
                    if (cb) cb.checked = true;
                }}""")

        # Click the "Select" button (btn-primary) in the modal to apply
        select_btn = self.page.locator(
            "#documentTypeModal-DocumentType a.btn-primary"
        )
        await select_btn.click()
        await self.page.wait_for_timeout(1000)

        # Verify textarea got populated
        ta_val = await self.page.evaluate(
            "document.querySelector('#documentType-DocumentType')?.value || ''"
        )
        _logger.info("Doc types selected: '%s'", ta_val[:100])

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

    async def _search_chunk_all_pages(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run `_search_chunk` then walk LandmarkWeb pagination until exhausted."""
        first_page = await self._search_chunk(date_from, date_to)
        all_records: list[ScrapedRecord] = list(first_page)
        seen_hashes: set[str] = {self.make_hash(r.to_dict()) for r in first_page}

        max_pages = 50  # safety cap
        for page_num in range(2, max_pages + 1):
            has_next = await self._go_next_page()
            if not has_next:
                _logger.info("No next page link — stopping at page %d", page_num - 1)
                break

            await self.page.wait_for_timeout(4000)
            page_records = await self._extract_results()
            if not page_records:
                _logger.info("Page %d empty — stopping", page_num)
                break

            new_count = 0
            for rec in page_records:
                h = self.make_hash(rec.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_records.append(rec)
                    new_count += 1

            _logger.info("Page %d — %d new (chunk total %d)", page_num, new_count, len(all_records))
            if new_count == 0:
                break

        return all_records

    async def _go_next_page(self) -> bool:
        """Click the LandmarkWeb Next page button if enabled."""
        try:
            next_btn = self.page.locator(
                "a:has-text('Next'), button:has-text('Next'), "
                "a[title*='Next'], .pagination .next a, "
                "[aria-label='Next']"
            )
            if await next_btn.count() == 0:
                return False
            first = next_btn.first
            disabled = await first.get_attribute("disabled")
            cls = await first.get_attribute("class") or ""
            if disabled or "disabled" in cls.lower():
                return False
            await first.click()
            return True
        except Exception as exc:
            _logger.info("Next page click failed: %s", str(exc)[:80])
            return False

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
                // Known positions (from Clark's structure).
                // Party cells: read innerHTML so the structural
                // <div class='nameSeperator'></div> survives to Python's
                // normalize_party_text(). textContent would collapse
                // stacked co-owners in-browser before we can split them.
                if (cells[5]) grantor = cells[5].innerHTML.trim();
                if (cells[6]) grantee = cells[6].innerHTML.trim();
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

        # Clark's portal-side Custom Selection filter is unreliable — the
        # search returns every document type regardless of what we put in
        # the textarea. Apply the doc-type filter client-side against the
        # extracted docType cell to guarantee we only return matching records.
        allowed_types_upper = [t.upper() for t in self._doc_types]

        dropped_no_pid = 0
        dropped_wrong_doctype = 0
        doc_type_counter: dict[str, int] = {}
        sample_legal: list[str] = []
        for item in raw:
            item_doc_type = (item.get("docType") or "").strip().upper()
            doc_type_counter[item_doc_type] = doc_type_counter.get(item_doc_type, 0) + 1

            # Skip rows whose docType doesn't match any configured keyword
            matched = False
            for keyword in allowed_types_upper:
                if keyword and keyword in item_doc_type:
                    matched = True
                    break
            if not matched:
                dropped_wrong_doctype += 1
                continue

            legal = (item.get("legal") or "").strip()
            pid_match = _PID_PATTERN.search(legal)
            if not pid_match:
                pid_match = _PID_FALLBACK_PATTERN.search(legal)
            if not pid_match and not self._skip_pid:
                dropped_no_pid += 1
                if len(sample_legal) < 3 and legal:
                    sample_legal.append(legal[:120])
                continue

            record = ScrapedRecord()
            record.parcel_id = pid_match.group(1) if pid_match else None
            # Party names: normalize stacked owners (nameSeperator div) -> " / "
            grantor = normalize_party_text(item.get("grantor"))
            grantee = normalize_party_text(item.get("grantee"))
            doc_type = (item.get("docType") or "").strip()

            # Pre-foreclosure correctness (same as the recorder templates): drop
            # cancelled/cured/trustee-admin docs, and orient the BORROWER (person)
            # into party_name — a Notice of Trustee's Sale is indexed with the
            # trustee company as grantor, so without this the trustee corp becomes
            # the lead. Only for pre_foreclosure; probate/divorce unchanged.
            if self._record_type == "pre_foreclosure":
                if is_cancellation_or_admin(doc_type):
                    continue
                oriented = orient_pre_foreclosure_party(grantor, grantee)
                if oriented is None:
                    continue
                grantor, grantee = oriented

            record.party_name = grantor
            record.heirs = grantee
            record.doc_type = doc_type
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

        _logger.info(
            "Records kept: %d / %d (dropped_wrong_doctype=%d, dropped_no_pid=%d)",
            len(records), len(raw), dropped_wrong_doctype, dropped_no_pid,
        )
        # Log top 5 doc types seen so we can tune _DOC_TYPES if probate variants are missed
        top_types = sorted(doc_type_counter.items(), key=lambda kv: -kv[1])[:5]
        if top_types:
            _logger.info("  Top doc types in page: %s",
                          ", ".join(f"{t}={n}" for t, n in top_types))
        if sample_legal:
            for i, s in enumerate(sample_legal):
                _logger.info("  Sample dropped legal[%d]: %s", i, s)
        return records
