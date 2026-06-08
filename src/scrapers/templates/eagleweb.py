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
from src.scrapers.base_scraper import (
    BridgeScraper,
    ScrapedRecord,
    normalize_party_text,
)
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.template.eagleweb")

# Document type keywords in EagleWeb checkbox labels
_DOC_TYPE_MAP = {
    "probate": ["PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
                "PERSONAL REPRESENTATIVE", "PERSONAL REP",
                "DEATH CERTIFICATE", "CERTIFICATE OF DEATH",
                "AFFIDAVIT OF HEIRSHIP", "TRANSFER ON DEATH",
                # Abbreviated codes (e.g. Clallam: DEATH, LETTR, EXEC, TOD, SUCC)
                "DEATH", "LETTR", "EXEC", "TOD", "SUCC"],
    "pre_foreclosure": ["LIS PENDENS", "NOTICE OF TRUSTEE SALE", "TRUSTEE SALE",
                        "TRUSTEE'S SALE", "NOTICE OF DEFAULT", "FORECLOSURE",
                        # Abbreviated codes (e.g. Clallam: LISP, NTS, NTSCL)
                        "LISP", "NTS"],
    "tax_delinquent": ["TAX LIEN", "CERTIFICATE OF DELINQUENCY",
                       "TAX DELINQUENT", "CERTIFICATE OF SALE"],
    "divorce": ["DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION",
                # Abbreviated codes
                "DISS", "DISOL"],
}

# Doc type phrases that LOOK like a match but document the opposite.
# Checked AFTER _DOC_TYPE_MAP — any match here drops the record.
_DOC_TYPE_EXCLUDE = {
    "probate": ["LACK OF PROBATE"],  # affidavit stating there is NO probate
}


class EagleWebScraper(BridgeScraper):
    """Template scraper for all Tyler EagleWeb recorder sites.

    Zero Claude AI cost — uses standardized selectors for the shared
    EagleWeb interface used by 16+ WA counties.
    """

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        require_parcel_id: bool | None = None,
    ):
        super().__init__()
        self.base_url = base_url
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        # Parcel-ID requirement policy:
        #   - Probate records are estate/name filings (Cert of Death, Letters
        #     Testamentary, Personal Rep Deed, Transfer-on-Death, etc.) that
        #     often carry no parcel in the recording index. Dropping them
        #     deletes most of the signal — name-only leads are still
        #     actionable via skip trace. Default OFF for probate.
        #   - Property-linked types (pre_foreclosure, tax_delinquent) have
        #     a parcel in every record; a missing parcel there means the
        #     extraction regex failed, so dropping is correct. Default ON.
        #   - Explicit caller override (True/False) always wins.
        if require_parcel_id is None:
            require_parcel_id = self.active_record_type not in ("probate", "divorce")
        self.require_parcel_id = require_parcel_id

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

            # Fill dates for this chunk — use the caller-requested record type
            # so the filter below only keeps documents matching THAT type.
            active_type = self.active_record_type or "all"
            await self._configure_search(active_type, cf, ct)

            # Submit and extract
            await self._submit_search()
            chunk_records = await self._extract_all_pages()

            new_count = self.dedupe_extend(chunk_records, seen_hashes, all_records)
            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new_count, len(all_records))

            chunk_start = chunk_end
            await self.polite_delay()

        # Drop records without parcel_id if required — these are upstream
        # data gaps (e.g., Thurston death certificates with empty Parcel
        # field), not scraper bugs, and they cannot be GIS-enriched.
        if self.require_parcel_id:
            before = len(all_records)
            all_records = [r for r in all_records if r.parcel_id]
            dropped = before - len(all_records)
            if dropped:
                _logger.info(
                    "Dropped %d/%d records with no parcel_id (upstream data gap)",
                    dropped, before,
                )

        # Records are returned raw; the run_scrape_job worker enriches
        # them inline (county_gis + AI assessor) before saving Result rows.
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
                    timeout=30_000,  # working installs redirect in seconds; the
                    # form.submit() fallback below backs up any slow click.
                    wait_until="domcontentloaded",
                ):
                    await submit.last.click()
                _logger.info("Search submitted via expect_navigation, page: %s", self.page.url)
            except Exception:
                # expect_navigation timed out on POST page.
                # Poll for the results link or URL change (server is processing).
                _logger.info("Navigation timeout on POST, polling for results...")
                for poll in range(12):  # up to 60s
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
                    # Some installs (e.g. Spokane) never auto-redirect on click —
                    # they stick on the intermediate docSearchPOST.jsp. Stop
                    # polling early and let the form.submit() fallback handle it.
                    if poll >= 1 and "docSearchPOST" in self.page.url:
                        _logger.info("Stuck on docSearchPOST after %ds; using form.submit() fallback", (poll+1)*5)
                        break

            # Fallback: if a button click never reached the results page, submit
            # the form directly. form.submit() follows the POST→redirect chain
            # (per this method's documented design) where a click can stick on
            # docSearchPOST.jsp. Guarded to fire ONLY while stuck on the search
            # POST/form pages (never on a results page or any other page), so
            # counties that redirect on click are unaffected and we never submit
            # an unrelated form.
            _url = self.page.url
            stuck_on_search = ("docSearchResults" not in _url) and (
                "docSearchPOST" in _url or "docSearch.jsp" in _url
            )
            if stuck_on_search:
                _logger.info("Not on results (url=%s); retrying via form.submit()", _url)
                try:
                    async with self.page.expect_navigation(
                        url="**/docSearchResults*", timeout=60_000, wait_until="domcontentloaded"
                    ):
                        await self.page.evaluate(
                            """() => {
                                const btn = document.querySelector('input[type=submit][value=Search]');
                                const form = (btn && btn.form) || document.querySelector('form');
                                if (form) form.submit();
                            }"""
                        )
                    _logger.info("form.submit() reached results: %s", self.page.url)
                except Exception:
                    _logger.info("form.submit() fallback did not reach results: %s", self.page.url)

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

            # Fetch parcel IDs from detail pages via HTTP (fast, concurrent)
            missing_parcel_total = sum(1 for r in new_records if not r.parcel_id)
            needs_parcel = [r for r in new_records if not r.parcel_id and r.enrichment_data.get("detail_href")]
            no_href_count = missing_parcel_total - len(needs_parcel)
            if no_href_count:
                _logger.info("  %d records missing parcel but no detail_href (cannot HTTP fetch)", no_href_count)
            if needs_parcel:
                _logger.info("  Fetching parcel IDs for %d records via HTTP...", len(needs_parcel))
                await self._fetch_parcel_ids_from_details(needs_parcel)

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
                        // Party names (Grantor/Grantee) live inside this summary
                        // cell and can stack multiple co-owners separated by
                        // structural markup (<br>, nameSeperator). textContent
                        // collapses those to a space, concatenating distinct
                        // owners. Capture innerHTML too so the Python side can
                        // run normalize_party_text() (separators -> " / ") before
                        // the Grantor/Grantee regex. summary stays flat for the
                        // date / parcel / legal-description parsing.
                        const summaryHtml = cells[1].innerHTML.trim();
                        if (!desc || desc.length < 3) continue;
                        // Skip rows that are scripts or navigation (no date pattern)
                        if (desc.includes('function ') || desc.includes('var ')) continue;
                        // Valid rows have a date in summary (MM/DD/YYYY)
                        if (!/\d{1,2}\/\d{1,2}\/\d{4}/.test(summary)) continue;
                        // Capture detail link from first cell (instrument number is a link)
                        const link = cells[0].querySelector('a[href*="docDetail"], a[href*="Detail"], a[href*="viewDoc"]');
                        const detailHref = link ? link.href : '';
                        // Extract parcel from its column if available
                        let parcel = '';
                        if (headerMap.parcel !== undefined && cells[headerMap.parcel]) {
                            parcel = cells[headerMap.parcel].textContent.trim();
                        }
                        results.push({desc, summary, summaryHtml, detailHref, parcel});
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
            parcel_source_counts = {"table": 0, "summary_labeled": 0, "summary_digit": 0, "none_yet": 0}
            for item in raw:
                desc = item.get("desc", "")
                summary = item.get("summary", "")
                # Separator-preserving copy of the summary cell: structural
                # markup (<br>, nameSeperator) that stacks multiple co-owners in
                # the Grantor/Grantee portion becomes " / " instead of being
                # collapsed to a space. Falls back to the flat summary if no HTML
                # was captured. Date / parcel / legal parsing still uses `summary`.
                party_summary = normalize_party_text(item.get("summaryHtml", "")) or summary
                detail_href = item.get("detailHref", "")
                table_parcel = item.get("parcel", "").strip()

                record = ScrapedRecord()

                # Use parcel from results table if available (avoids detail page clicks)
                if table_parcel and re.match(r"\d{4,}[\.\d]*", table_parcel):
                    record.parcel_id = table_parcel
                    parcel_source_counts["table"] += 1

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

                # Legal description keywords that appear AFTER party names in the
                # summary cell. Without these as stop markers, the Grantor regex
                # captures "FORD, MICHAEL M ESTSubdivision HIDDEN EST Lot 5..."
                # because probate records often have no Grantee: label.
                _LEGAL_STOP = r"(?:Grantee:|Legal[:\s]|Subdivision|Section\s*:|Section\s+\d|Lot\s+\d|Block\s+\d|Parcel[:\s]|Plat\s|Tract\s|$)"

                # Parse Grantor (from the separator-preserving party_summary so
                # stacked co-owners keep their " / " boundary). Re-run
                # normalize_party_text on the CAPTURED field so the field-boundary
                # delimiter that may trail the value (e.g. a "<br>" between the
                # Grantor and Grantee fields became " / ") is trimmed — without
                # this the value could end in a stray " /" (Codex review).
                grantor_match = re.search(r"Grantor:\s*(.+?)" + _LEGAL_STOP, party_summary, re.DOTALL)
                if grantor_match:
                    record.party_name = normalize_party_text(grantor_match.group(1)).rstrip(",")

                # Parse Grantee
                grantee_match = re.search(r"Grantee:\s*(.+?)(?:Grantor:|" + _LEGAL_STOP[4:], party_summary, re.DOTALL)
                if grantee_match:
                    grantee = normalize_party_text(grantee_match.group(1)).rstrip(",").rstrip(".")
                    if grantee:
                        record.heirs = grantee

                # Parcel ID — check both summary and description (if not already from table)
                combined = f"{summary} {desc}"
                if not record.parcel_id:
                    # Try "Parcel: XXXXX" or "Parcel:XXXXX" pattern first
                    parcel_labeled = re.search(r"[Pp]arcel[:\s]+(\d{4,}\.\d{3,}|\d{5,})", combined)
                    if parcel_labeled:
                        # Reject if Kitsap-style truncation marker follows
                        # the digits (e.g. "Parcel: 43200000080..." — Kitsap
                        # hides the last digits in the results table for
                        # records still in workflow processing).
                        next_chars = combined[parcel_labeled.end():parcel_labeled.end() + 3]
                        if next_chars.startswith("..."):
                            parcel_source_counts["none_yet"] += 1
                        else:
                            record.parcel_id = parcel_labeled.group(1)
                            parcel_source_counts["summary_labeled"] += 1
                    else:
                        # Whitman-style dash-segmented parcel (digit or letter prefix):
                        #   2-0000-44-14-25-3390  (digit-prefixed, 6 segments)
                        #   L-0985-00-00-27-0000  (letter-prefixed, 6 segments)
                        #   8-0800-00-00-0013     (5 segments)
                        # The first-pass digit regex above requires 5+ consecutive
                        # digits, which whitman parcels never have since they max
                        # out at 4 digits per segment.
                        #
                        # Whitman's legal-description field often contains a
                        # TRUNCATED copy of the parcel followed by "..." before
                        # the full parcel appears later in the summary. Walk all
                        # matches and skip any immediately followed by "...".
                        dash_pattern = re.compile(
                            r"[Pp]arcel[:\s]+([A-Z]-\d+(?:-\d+){2,}|\d-\d+(?:-\d+){2,})"
                        )
                        for m in dash_pattern.finditer(combined):
                            tail = combined[m.end():m.end() + 3]
                            if tail.startswith("..."):
                                continue  # truncated — keep scanning
                            record.parcel_id = m.group(1)
                            parcel_source_counts["summary_labeled"] += 1
                            break

                        if not record.parcel_id:
                            # Fallback: any standalone 10+ digit number (not an instrument/year prefix)
                            # Skip this fallback entirely if "Related:" appears in the
                            # text — Kitsap (and others) list related document numbers
                            # there which look like parcels but aren't.
                            if "Related:" in combined or "related:" in combined:
                                parcel_source_counts["none_yet"] += 1
                            else:
                                parcel_match = re.search(r"\b(\d{10,})\b", combined)
                                if parcel_match:
                                    pid = parcel_match.group(1)
                                    # Skip instrument numbers (start with 2024/2025/2026)
                                    if not pid[:4] in ("2024", "2025", "2026"):
                                        record.parcel_id = pid
                                        parcel_source_counts["summary_digit"] += 1
                                    else:
                                        parcel_source_counts["none_yet"] += 1
                                else:
                                    parcel_source_counts["none_yet"] += 1

                # Also store the legal description text — stop before
                # Grantor/Grantee labels and doc type names that follow.
                legal_text = re.search(
                    r"(?:Subdivision|Section[\s:]+\d|Lot|Block|Plat|Tract)\s*.+?(?=\s*(?:Grant(?:or|ee):|Notice\s|Affidavit|Deed|Warranty|Quit\s*Claim|$))",
                    combined, re.IGNORECASE,
                )
                if legal_text and not record.legal_description:
                    record.legal_description = legal_text.group(0).strip()[:200]

                if record.party_name or record.date_recorded:
                    # Filter by the caller-requested record type ONLY. Previously
                    # this loop OR-combined keywords across self.record_types,
                    # so a user asking for "probate" on a ['probate','pre_foreclosure']
                    # connector would get both types mixed. Now active_record_type
                    # is the single-type request from tasks.py (or the first entry
                    # in record_types for legacy probe scripts).
                    active_rt = self.active_record_type
                    if active_rt and active_rt != "all":
                        doc_upper = desc.upper()
                        kws = _DOC_TYPE_MAP.get(active_rt, [])
                        if not any(kw in doc_upper for kw in kws):
                            continue  # Skip non-matching doc types
                        excludes = _DOC_TYPE_EXCLUDE.get(active_rt, [])
                        if any(neg in doc_upper for neg in excludes):
                            continue  # e.g. "LACK OF PROBATE AFFIDAVIT"
                    # Phase 2a: capture the document type so it reaches Result/export.
                    if desc:
                        record.doc_type = desc.strip()[:128]
                    records.append(record)

            _logger.info(
                "Extracted %d records from page — parcel sources: table=%d summary_labeled=%d summary_digit=%d needs_detail_fetch=%d",
                len(records),
                parcel_source_counts["table"],
                parcel_source_counts["summary_labeled"],
                parcel_source_counts["summary_digit"],
                parcel_source_counts["none_yet"],
            )

        except Exception as exc:
            _logger.warning("Error extracting page: %s", str(exc)[:80])

        return records

    async def _fetch_parcel_ids_from_details(self, records: list[ScrapedRecord]) -> None:
        """Fetch parcel IDs from EagleWeb detail pages via HTTP (not Playwright).

        Uses concurrent HTTP requests to fetch detail pages — 10x faster than
        clicking through the browser. Extracts parcel IDs from the HTML with regex.
        Does NOT navigate the browser, so pagination state is preserved.
        """
        import re as _re
        import asyncio as _asyncio
        from concurrent.futures import ThreadPoolExecutor

        _PARCEL_PATTERNS = [
            # Dotted format (Spokane: 44042.0202) and plain digits
            _re.compile(r'[Pp]arcel[:\s]*\s*(\d{4,}\.\d{3,}|\d{5,})'),
            # Whitman dash-segmented: 2-0000-44-14-25-3390 or L-0985-00-00-27-0000
            _re.compile(r'[Pp]arcel[:\s]*\s*([A-Z]-\d+(?:-\d+){2,}|\d-\d+(?:-\d+){2,})'),
            _re.compile(r'[Tt]ax\s*(?:[#:]|ID[:\s]|Number[:\s])\s*(\d{4,}\.\d{3,}|\d{5,})'),
            _re.compile(r'APN[:\s]+(\d{4,}\.\d{3,}|\d{5,})'),
        ]
        _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}

        # Get browser cookies for authenticated session
        cookies = await self.page.context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        base = self.base_url  # pin detail fetches to the portal origin

        def _fetch_one(href: str) -> str | None:
            """Fetch a detail page via HTTP and extract parcel ID."""
            try:
                # SSRF-safe: the detail href is scraped from page DOM. safe_get
                # validates (DNS-rebinding aware) and same_origin_as=base ensures
                # the authenticated session cookies are only ever sent to the
                # portal's own origin — never a host injected into the page.
                resp = safe_get(
                    href,
                    same_origin_as=base,
                    require_allowlisted=True,
                    headers=_HEADERS,
                    cookies=cookie_dict,
                    timeout=8,
                )
                if resp.status_code != 200:
                    return None
                text = resp.text

                # Try regex patterns on HTML text
                for pat in _PARCEL_PATTERNS:
                    m = pat.search(text)
                    if m:
                        return m.group(1)

                # Try finding parcel in Legal Description section
                legal_idx = text.find("Legal")
                if legal_idx == -1:
                    legal_idx = text.find("legal")
                if legal_idx > -1:
                    section = text[legal_idx:legal_idx + 1000]
                    m = _re.search(r'\b(\d{10,})\b', section)
                    if m and not m.group(1).startswith(("2024", "2025", "2026")):
                        return m.group(1)
            except Exception:
                pass
            return None

        # Run HTTP fetches concurrently (10 at a time)
        found = 0
        items = [(r, r.enrichment_data.get("detail_href", "")) for r in records if r.enrichment_data.get("detail_href")]

        if not items:
            _logger.info("  No detail URLs to fetch")
            return

        loop = _asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Batch in groups of 50 to avoid overwhelming the server
            for batch_start in range(0, len(items), 50):
                batch = items[batch_start:batch_start + 50]
                tasks = [loop.run_in_executor(executor, _fetch_one, href) for _, href in batch]
                results_list = await _asyncio.gather(*tasks, return_exceptions=True)

                for (record, _), parcel in zip(batch, results_list):
                    if isinstance(parcel, str) and parcel:
                        record.parcel_id = parcel
                        found += 1

        _logger.info("  Detail pages (HTTP): found %d parcel IDs from %d lookups", found, len(items))

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
