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
        """Scrape records from an EagleWeb site using date chunking.

        EagleWeb's POST page takes too long to redirect for large date ranges.
        Split the range into 7-day chunks — each chunk searches, extracts,
        then navigates back to the search form for the next chunk.

        Args:
            date_from: Start date in MM/DD/YYYY format.
            date_to: End date in MM/DD/YYYY format.

        Returns:
            List of ScrapedRecord instances.
        """
        from datetime import datetime, timedelta

        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7  # 7-day chunks keep result sets small

        _logger.info(
            "EagleWeb scraper — %s/%s — %s to %s (%d-day chunks)",
            self.county, self.state, date_from, date_to, chunk_days,
        )

        await self.navigate(self.base_url)

        # Step 1: Accept disclaimer (only once)
        await self._accept_disclaimer()

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            # Navigate back to search form for each chunk
            if chunk_start != start:
                # Try "New Search" link first (stays in session)
                navigated = False
                try:
                    new_search = self.page.locator(
                        "a:has-text('New Search'), a:has-text('DOCUMENT SEARCH'), "
                        "a:has-text('Modify Search'), a[href*='docSearch.jsp']"
                    )
                    if await new_search.count() > 0:
                        await new_search.first.click()
                        await self.page.wait_for_timeout(2_000)
                        navigated = True
                except Exception:
                    pass

                if not navigated:
                    # Build docSearch.jsp URL from current URL
                    current = self.page.url
                    if "/eagleweb/" in current:
                        base = current.split("/eagleweb/")[0]
                        search_url = f"{base}/eagleweb/docSearch.jsp"
                    else:
                        search_url = self.base_url
                    await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15_000)
                    await self.page.wait_for_timeout(2_000)
                    # Accept disclaimer if shown
                    await self._accept_disclaimer()

                # Wait for search form to be ready
                try:
                    await self.page.wait_for_selector(
                        "#RecDateIDStart, #recordingDateIDStart, #StartDate",
                        timeout=10_000,
                    )
                except Exception:
                    _logger.warning("Date input not found after navigation back")

            # Fill dates for this chunk
            await self._configure_search("all", cf, ct)

            # Submit and extract
            await self._submit_search()
            chunk_records = await self._extract_all_pages()

            # Deduplicate against all records
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

        # No inline enrichment — records are enriched in a separate background task
        # (enrich_job_results) after the job completes. This keeps scraping fast (~5 min).
        _logger.info("EagleWeb complete — %d records (enrichment runs after save)", len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Click 'I Acknowledge' disclaimer if present.

        Uses Playwright's native .click() (not JS el.click()) so form
        submission + navigation fires correctly on every EagleWeb variant.
        """
        try:
            # Step 1: Try disclaimer buttons (various EagleWeb labels)
            # Priority order: Acknowledge > Accept > Agree > Enter
            disclaimer_btn = self.page.locator(
                "input[type='submit'][value*='Acknowledge' i], "
                "button:has-text('Acknowledge'), "
                "input[type='submit'][value*='Accept' i], "
                "button:has-text('Accept'), "
                "a:has-text('Accept'), "
                "input[type='submit'][value*='Agree' i], "
                "button:has-text('Agree'), "
                "input[type='submit'][value='Enter'], "
                "input[type='submit'][value='ENTER']"
            )
            if await disclaimer_btn.count() > 0:
                btn_text = await disclaimer_btn.first.get_attribute("value") or ""
                _logger.info("Disclaimer button found: '%s'", btn_text)
                try:
                    async with self.page.expect_navigation(timeout=15_000):
                        await disclaimer_btn.first.click()
                    _logger.info("Disclaimer accepted via navigation, now at: %s", self.page.url)
                except Exception:
                    # Navigation event may not fire on all sites
                    await self.page.wait_for_timeout(3_000)
                    _logger.info("Disclaimer clicked (no nav event), now at: %s", self.page.url)

                # Step 2: Some EagleWeb sites need a second click (Login → Public Login)
                public_btn = self.page.locator(
                    "input[type='submit'][value*='Public Login' i], "
                    "button:has-text('Public Login'), "
                    "input[type='submit'][value*='Public Log In' i]"
                )
                if await public_btn.count() > 0:
                    _logger.info("Found 'Public Login' button, clicking step 2")
                    try:
                        async with self.page.expect_navigation(timeout=15_000):
                            await public_btn.first.click()
                        _logger.info("Public login accepted, now at: %s", self.page.url)
                    except Exception:
                        await self.page.wait_for_timeout(3_000)
                return

            # Step 2 fallback: Try "Public Login" directly (Spokane-style)
            public_btn = self.page.locator(
                "input[type='submit'][value*='Public Login' i], "
                "button:has-text('Public Login')"
            )
            if await public_btn.count() > 0:
                _logger.info("Found 'Public Login' button (no disclaimer)")
                try:
                    async with self.page.expect_navigation(timeout=15_000):
                        await public_btn.first.click()
                    _logger.info("Public login clicked, now at: %s", self.page.url)
                except Exception:
                    await self.page.wait_for_timeout(3_000)
                return

            # Step 3: Try generic Login button
            login_btn = self.page.locator(
                "input[type='submit'][value='Login'], "
                "input[type='submit'][value='Log In'], "
                "button:has-text('Login')"
            )
            if await login_btn.count() > 0:
                _logger.info("Found generic 'Login' button")
                try:
                    async with self.page.expect_navigation(timeout=15_000):
                        await login_btn.first.click()
                    _logger.info("Login clicked, now at: %s", self.page.url)
                except Exception:
                    await self.page.wait_for_timeout(3_000)

                # After generic Login, check for Public Login (Spokane-style 2-step)
                await self.page.wait_for_timeout(1_000)
                public_btn2 = self.page.locator(
                    "input[type='submit'][value*='Public Login' i], "
                    "button:has-text('Public Login'), "
                    "input[type='submit'][value*='Public Log In' i]"
                )
                if await public_btn2.count() > 0:
                    _logger.info("Found 'Public Login' after generic Login, clicking...")
                    try:
                        async with self.page.expect_navigation(timeout=15_000):
                            await public_btn2.first.click()
                        _logger.info("Public login accepted, now at: %s", self.page.url)
                    except Exception:
                        await self.page.wait_for_timeout(3_000)
                return

            _logger.info("No disclaimer found, continuing")
        except Exception as exc:
            _logger.info("No disclaimer found: %s", str(exc)[:80])

    async def _configure_search(self, record_type: str, date_from: str, date_to: str) -> None:
        """Configure EagleWeb search form.

        Strategy: Keep "Search All Types" checked and filter by doc type
        during extraction. This is more reliable than trying to check/uncheck
        individual type checkboxes (which vary per county).
        """
        # Leave "Search All Types" checked — filter by type during extraction
        _logger.info("Searching all types, will filter '%s' during extraction", record_type)

        # Fill dates using pressSequentially (simulates real keystrokes).
        # This is critical — fill() doesn't trigger EagleWeb's internal JS
        # event handlers, but pressSequentially does.
        try:
            # Wait for the search form to load (date inputs may take a moment)
            try:
                await self.page.wait_for_selector(
                    "#RecDateIDStart, #recordingDateIDStart, #StartDate",
                    timeout=10_000,
                )
            except Exception:
                # Try alternate selector
                try:
                    await self.page.wait_for_selector(
                        "input[name*='Date'][name*='Start'], input[name*='recording']",
                        timeout=5_000,
                    )
                except Exception:
                    pass

            filled = False
            for start_id, end_id in [
                ("RecDateIDStart", "RecDateIDEnd"),
                ("recordingDateIDStart", "recordingDateIDEnd"),
                ("StartDate", "EndDate"),
            ]:
                start_el = self.page.locator(f"#{start_id}")
                end_el = self.page.locator(f"#{end_id}")
                if await start_el.count() > 0 and await end_el.count() > 0:
                    # Clear and type start date
                    await start_el.click()
                    await start_el.fill("")  # clear first
                    await start_el.press_sequentially(date_from, delay=30)
                    # Clear and type end date
                    await end_el.click()
                    await end_el.fill("")  # clear first
                    await end_el.press_sequentially(date_to, delay=30)
                    filled = True
                    _logger.info("Date range typed: %s to %s", date_from, date_to)
                    break

            if not filled:
                # Fallback: find pre-filled date inputs
                inputs = await self.page.locator("input[type='text']").all()
                date_inputs = []
                for inp in inputs:
                    val = await inp.get_attribute("value") or ""
                    if "/" in val and len(val) >= 8:
                        date_inputs.append(inp)
                if len(date_inputs) >= 2:
                    await date_inputs[0].click()
                    await date_inputs[0].fill("")
                    await date_inputs[0].press_sequentially(date_from, delay=30)
                    await date_inputs[1].click()
                    await date_inputs[1].fill("")
                    await date_inputs[1].press_sequentially(date_to, delay=30)
                    filled = True
                    _logger.info("Date range typed (fallback): %s to %s", date_from, date_to)

            if not filled:
                _logger.warning("Could not find date inputs to fill")
        except Exception as exc:
            _logger.warning("Could not set date range: %s", str(exc)[:60])

    async def _select_doc_types(self, record_type: str) -> None:
        """Select document type checkboxes matching the record type via JavaScript."""
        keywords = _DOC_TYPE_MAP.get(record_type, [])
        if not keywords:
            return

        # Use JavaScript to find and check matching checkboxes
        # EagleWeb checkboxes have labels as sibling text nodes
        keywords_json = str(keywords)
        selected = await self.page.evaluate(f"""
            (() => {{
                const keywords = {keywords_json};
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                let count = 0;
                for (const cb of checkboxes) {{
                    if (cb.id === 'allTypes' || cb.name === 'allTypes') continue;
                    const parent = cb.parentElement;
                    if (!parent) continue;
                    const text = parent.textContent.trim().toUpperCase();
                    for (const kw of keywords) {{
                        if (text.includes(kw)) {{
                            if (!cb.checked) {{
                                cb.checked = true;
                                cb.dispatchEvent(new Event('change', {{bubbles: true}}));
                                cb.dispatchEvent(new Event('click', {{bubbles: true}}));
                                count++;
                            }}
                            break;
                        }}
                    }}
                }}
                return count;
            }})()
        """)
        _logger.info("Selected %d doc type checkboxes for %s", selected, record_type)

    async def _submit_search(self) -> None:
        """Submit the search form using form.submit() for reliable redirect.

        form.submit() follows the POST→redirect chain correctly,
        unlike clicking the submit button which gets stuck on the
        intermediate docSearchPOST.jsp page in headless mode.
        """
        try:
            submit = self.page.locator("input[type='submit'][value='Search']")
            if await submit.count() == 0:
                submit = self.page.locator("button:has-text('Search')")

            # Click submit and explicitly wait for navigation to complete.
            # Use expect_navigation to properly catch the POST→redirect chain.
            try:
                async with self.page.expect_navigation(
                    url="**/docSearchResults*",
                    timeout=120_000,  # 2 min — large date ranges take time
                    wait_until="domcontentloaded",
                ):
                    await submit.last.click()
                _logger.info("Search submitted via expect_navigation, page: %s", self.page.url)
            except Exception:
                # expect_navigation timed out on POST page.
                # Poll for the results link or URL change (server is processing).
                _logger.info("Navigation timeout on POST, polling for results...")
                for poll in range(30):  # 30 x 5s = 150s max
                    await self.page.wait_for_timeout(5_000)
                    # Check if redirected
                    if "Results" in self.page.url or "results" in self.page.url:
                        _logger.info("Redirected to results after %ds", (poll+1)*5)
                        break
                    # Check for results link
                    results_link = self.page.locator("a[href*='docSearchResults']")
                    if await results_link.count() > 0:
                        _logger.info("Found results link after %ds, clicking", (poll+1)*5)
                        await results_link.first.click()
                        await self.page.wait_for_timeout(3_000)
                        break

            await self.page.wait_for_timeout(2_000)
            _logger.info("Final page: %s", self.page.url)
        except Exception as exc:
            _logger.warning("Could not submit search: %s", str(exc)[:60])

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages, then fetch missing parcel IDs."""
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1

            records = await self._extract_page()
            new_count = 0
            new_records: list[ScrapedRecord] = []
            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_records.append(record)
                    new_count += 1

            _logger.info("Page %d — %d new records (total: %d)", page_num, new_count, len(all_records))

            if new_count == 0:
                break

            # Fetch parcel IDs from detail pages — capped at 20 per page
            # to balance data quality vs speed (~2 min per page with 20 clicks)
            _MAX_DETAIL_CLICKS = 20
            needs_parcel = [r for r in new_records if not r.parcel_id and r.enrichment_data.get("detail_href")]
            if needs_parcel:
                batch = needs_parcel[:_MAX_DETAIL_CLICKS]
                _logger.info("  Detail lookup: %d/%d records (capped at %d)",
                             len(batch), len(needs_parcel), _MAX_DETAIL_CLICKS)
                await self._fetch_parcel_ids_from_details(batch)

            # Check for Next page link
            has_next = await self._go_next_page()
            if not has_next:
                break

            await self.polite_delay()

        parcels_found = sum(1 for r in all_records if r.parcel_id)
        _logger.info("Parcel IDs found: %d/%d (%.0f%%)", parcels_found, len(all_records),
                     (parcels_found / len(all_records) * 100) if all_records else 0)
        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current EagleWeb results page via JavaScript.

        Uses browser-side JS extraction instead of BeautifulSoup for reliability.
        EagleWeb results: Description (doc type + AFN) | Summary (date + Grantor + Grantee)
        Also captures detail page links for parcel ID extraction.
        """
        records: list[ScrapedRecord] = []

        try:
            # Extract directly via JavaScript — more reliable than BeautifulSoup
            # Also capture detail page links (href containing docDetail) for parcel lookups
            raw = await self.page.evaluate("""
                (() => {
                    const tables = document.querySelectorAll('table');
                    let bestTable = null, bestRows = 0;
                    for (const t of tables) {
                        const ths = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim().toUpperCase());
                        if (ths.includes('DESCRIPTION') || ths.includes('SUMMARY')) {
                            const rows = t.querySelectorAll('tr').length;
                            if (rows > bestRows) { bestRows = rows; bestTable = t; }
                        }
                    }
                    if (!bestTable) return [];

                    // Build header-to-index map for flexible column extraction
                    const ths = Array.from(bestTable.querySelectorAll('th'));
                    const headerMap = {};
                    ths.forEach((th, idx) => {
                        const t = th.textContent.trim().toUpperCase();
                        if (t.includes('PARCEL')) headerMap.parcel = idx;
                        if (t.includes('LEGAL')) headerMap.legal = idx;
                        if (t.includes('REMARK')) headerMap.remarks = idx;
                    });

                    const results = [];
                    const rows = bestTable.querySelectorAll('tr');
                    for (let i = 1; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length < 2) continue;
                        const desc = cells[0].textContent.trim();
                        const summary = cells[1].textContent.trim();
                        if (!desc || desc.length < 3) continue;
                        // Skip rows that are scripts or navigation (no date pattern)
                        if (desc.includes('function ') || desc.includes('var ')) continue;
                        // Valid rows have a date in summary (MM/DD/YYYY)
                        if (!/\d{1,2}\/\d{1,2}\/\d{4}/.test(summary)) continue;
                        // Capture detail link from first cell (instrument number is a link)
                        const link = cells[0].querySelector('a[href*="docDetail"], a[href*="Detail"]');
                        const detailHref = link ? link.href : '';
                        // Extract parcel from its column if available
                        let parcel = '';
                        if (headerMap.parcel !== undefined && cells[headerMap.parcel]) {
                            parcel = cells[headerMap.parcel].textContent.trim();
                        }
                        results.push({desc, summary, detailHref, parcel});
                    }
                    return results;
                })()
            """)

            if not raw:
                page_text = await self.page.inner_text("body")
                if "No documents found" in page_text or "0 items found" in page_text:
                    _logger.info("No results found on this page")
                else:
                    _logger.warning("Could not find results table via JS")
                return []

            import re
            for item in raw:
                desc = item.get("desc", "")
                summary = item.get("summary", "")
                detail_href = item.get("detailHref", "")
                table_parcel = item.get("parcel", "").strip()

                record = ScrapedRecord()

                # Use parcel from results table if available (avoids detail page clicks)
                if table_parcel and re.match(r"\d{5,}", table_parcel):
                    record.parcel_id = table_parcel

                # Parse AFN from description and store in enrichment_data
                afn_match = re.search(r"\b(\d{5,})\b", desc)
                if afn_match:
                    record.enrichment_data["instrument_number"] = afn_match.group(1)
                    if detail_href:
                        record.enrichment_data["detail_href"] = detail_href

                # Parse date
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", summary)
                if date_match:
                    record.date_recorded = date_match.group(1)

                # Parse Grantor
                grantor_match = re.search(r"Grantor:\s*(.+?)(?:Grantee:|$)", summary, re.DOTALL)
                if grantor_match:
                    record.party_name = grantor_match.group(1).strip().rstrip(",")

                # Parse Grantee
                grantee_match = re.search(r"Grantee:\s*(.+?)(?:Grantor:|$)", summary, re.DOTALL)
                if grantee_match:
                    grantee = grantee_match.group(1).strip().rstrip(",").rstrip(".")
                    if grantee:
                        record.heirs = grantee

                # Parcel ID — check both summary and description
                combined = f"{summary} {desc}"
                # Try "Parcel: XXXXX" or "Parcel:XXXXX" pattern first
                parcel_labeled = re.search(r"[Pp]arcel[:\s]+(\d{5,})", combined)
                if parcel_labeled:
                    record.parcel_id = parcel_labeled.group(1)
                else:
                    # Fallback: any standalone 10+ digit number (not an instrument/year prefix)
                    parcel_match = re.search(r"\b(\d{10,})\b", combined)
                    if parcel_match:
                        pid = parcel_match.group(1)
                        # Skip instrument numbers (start with 2024/2025/2026)
                        if not pid[:4] in ("2024", "2025", "2026"):
                            record.parcel_id = pid

                # Also store the legal description text
                legal_text = re.search(r"(?:Subdivision|Section|Lot|Block|Plat|Tract)\s+.+", combined, re.IGNORECASE)
                if legal_text and not record.legal_description:
                    record.legal_description = legal_text.group(0).strip()[:200]

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:80])

        return records

    async def _fetch_parcel_ids_from_details(self, records: list[ScrapedRecord]) -> None:
        """Click into EagleWeb detail pages to extract parcel IDs.

        EagleWeb detail pages (docDetail.jsp) show full document info including
        the parcel/tax ID that isn't visible in search results.

        Strategy: click each detail link, extract parcel, use browser back to
        return to results. EagleWeb maintains pagination state across back navigation.
        """
        page = self.page
        found = 0
        results_url = page.url  # Save current results URL for recovery

        for record in records:
            href = record.enrichment_data.get("detail_href", "")
            inst = record.enrichment_data.get("instrument_number", "?")

            if not href:
                # No direct link — try clicking the instrument number text
                try:
                    link = page.locator(f"a:has-text('{inst}')").first
                    if await link.count() == 0:
                        continue
                    await link.click(timeout=8_000)
                    await page.wait_for_timeout(1_500)
                except Exception:
                    continue
            else:
                try:
                    await page.goto(href, wait_until="domcontentloaded", timeout=15_000)
                    await page.wait_for_timeout(1_000)
                except Exception as exc:
                    _logger.debug("Could not navigate to detail: %s", str(exc)[:50])
                    continue

            # Extract parcel ID from detail page
            try:
                parcel_id = await page.evaluate(r"""() => {
                    const body = document.body.innerText;

                    // Method 1: Look for "Parcel" or "Tax" labeled field
                    const labelPatterns = [
                        /[Pp]arcel\s*(?:[#:]|ID[:\s]|Number[:\s])\s*(\d{5,})/,
                        /[Tt]ax\s*(?:[#:]|ID[:\s]|Number[:\s])\s*(\d{5,})/,
                        /APN[:\s]+(\d{5,})/,
                    ];
                    for (const pat of labelPatterns) {
                        const m = body.match(pat);
                        if (m) return m[1];
                    }

                    // Method 2: Check table cells for "Parcel" label + adjacent value
                    const cells = document.querySelectorAll('td, th, dt, dd, span, div');
                    for (let i = 0; i < cells.length; i++) {
                        const text = cells[i].textContent.trim().toLowerCase();
                        if ((text.includes('parcel') || text.includes('tax id') || text.includes('apn'))
                            && cells[i+1]) {
                            const val = cells[i+1].textContent.trim();
                            const m = val.match(/(\d{5,})/);
                            if (m) return m[1];
                        }
                    }

                    // Method 3: Look for Legal Description section with parcel number
                    const legalIdx = body.indexOf('Legal');
                    if (legalIdx > -1) {
                        const section = body.substring(legalIdx, legalIdx + 1000);
                        const m = section.match(/\b(\d{10,})\b/);
                        if (m) {
                            // Skip year-like prefixes
                            if (!m[1].startsWith('2024') && !m[1].startsWith('2025') && !m[1].startsWith('2026'))
                                return m[1];
                        }
                    }

                    return null;
                }""")

                if parcel_id and parcel_id.strip():
                    record.parcel_id = parcel_id.strip()
                    found += 1
            except Exception:
                pass

            # Go back to results page
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                await page.wait_for_timeout(1_000)
            except Exception:
                # Recovery: navigate directly to saved results URL
                try:
                    await page.goto(results_url, wait_until="domcontentloaded", timeout=15_000)
                    await page.wait_for_timeout(1_500)
                except Exception:
                    _logger.warning("Could not return to results page — stopping detail fetch")
                    break

        _logger.info("  Detail pages: found %d parcel IDs from %d lookups", found, len(records))

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
