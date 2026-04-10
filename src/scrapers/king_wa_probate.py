"""King County (WA) Recorder — LandmarkWeb scraper for multiple record types.

Portal: https://recordsearch.kingcounty.gov/LandmarkWeb/search/index
Platform: Hyland LandmarkWeb

Flow:
1. Accept disclaimer (if present)
2. Click "Document Type Search" in left sidebar
3. Select document category from dropdown (#documentCategory-DocumentType)
4. Fill date range (#beginDate-DocumentType / #endDate-DocumentType)
5. Solve reCAPTCHA (manual in headed mode, or automated via service)
6. Click Submit (#submit-DocumentType)
7. Extract results: Recording #, Date, Grantor, Grantee, Legal
8. Filter for records with PID (Parcel ID) in the Legal column

Supported record types (subclass and set DOC_TYPE_SEARCH_TEXTS):
- Death Certificates (probate): grantor=deceased, grantee=heir
- Pre-foreclosure (NOD, Trustee Sale, Lis Pendens): grantor=borrower, grantee=lender
- Divorce (Decree of Dissolution): grantor=petitioner, grantee=respondent
"""

import asyncio
import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_wa_probate")

_BASE_URL = "https://recordsearch.kingcounty.gov/LandmarkWeb"
_SEARCH_URL = f"{_BASE_URL}/search/index"

# Regex to extract parcel ID from legal description (e.g. "PID:1234567890")
_PID_PATTERN = re.compile(r"PID[:\s]*(\d{6,12})", re.IGNORECASE)


class KingCountyLandmarkWebScraper(BridgeScraper):
    """King County LandmarkWeb Recorder scraper — supports multiple record types.

    Uses Document Type Search dropdown for precise category selection.
    Pass record_type to constructor to select which document category to scrape.

    Records without PID (parcel ID) in the Legal column are filtered out
    since they can't be enriched with property/mailing addresses.
    """

    # Maps record_type → (search_texts, label, grantor_label, grantee_label)
    RECORD_TYPE_CONFIG: dict[str, dict] = {
        "probate": {
            "search_texts": ["death cert"],
            "label": "DEATH CERTIFICATE",
            "grantor": "deceased",
            "grantee": "heir",
        },
        "death_certificate": {
            "search_texts": ["death cert"],
            "label": "DEATH CERTIFICATE",
            "grantor": "deceased",
            "grantee": "heir",
        },
        "pre_foreclosure": {
            "search_texts": ["notice of trustee sale"],
            "label": "PRE-FORECLOSURE",
            "grantor": "borrower",
            "grantee": "lender",
        },
        "divorce": {
            "search_texts": ["dissolution", "divorce", "decree"],
            "label": "DIVORCE",
            "grantor": "petitioner",
            "grantee": "respondent",
        },
    }

    def __init__(self, base_url: str | None = None, county: str = "king", state: str = "WA", record_type: str = "probate"):
        super().__init__()
        self._base_url = (base_url or _BASE_URL).rstrip("/")
        self._county = county
        self._state = state

        # Look up config for the requested record type
        cfg = self.RECORD_TYPE_CONFIG.get(record_type, self.RECORD_TYPE_CONFIG["probate"])
        self.DOC_TYPE_SEARCH_TEXTS = cfg["search_texts"]
        self.DOC_TYPE_LABEL = cfg["label"]
        self.GRANTOR_LABEL = cfg["grantor"]
        self.GRANTEE_LABEL = cfg["grantee"]

        from urllib.parse import urlparse
        domain = urlparse(self._base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 90  # ~120 results per chunk, 3 pages — fewer captcha solves

        total_chunks = max(1, (end - start).days // chunk_days + 1)

        _logger.info(
            "%s County %s — %s to %s (%d chunks of %d days)",
            self._county.title(), self.DOC_TYPE_LABEL, date_from, date_to, total_chunks, chunk_days,
        )

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        chunk_num = 0

        # Navigate and accept disclaimer — retry up to 3 times on crash
        search_url = f"{self._base_url}/search/index" if "/search/" not in self._base_url else self._base_url
        for attempt in range(1, 4):
            try:
                await self.navigate(search_url)
                await self._accept_disclaimer()
                await self._solve_captcha_once()
                break
            except Exception as exc:
                _logger.warning("Startup attempt %d/3 failed: %s", attempt, str(exc)[:80])
                if attempt == 3:
                    raise
                await asyncio.sleep(5)

        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            ct = chunk_end.strftime("%m/%d/%Y")
            chunk_num += 1

            _logger.info("Chunk %d/%d: %s to %s", chunk_num, total_chunks, cf, ct)

            try:
                records = await self._search_chunk(cf, ct)
            except Exception as exc:
                _logger.warning("Chunk %d failed: %s — skipping", chunk_num, str(exc)[:120])
                chunk_start = chunk_end
                continue

            new_count = 0
            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen:
                    seen.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info(
                "Chunk %d/%d done: %d new (total %d)",
                chunk_num, total_chunks, new_count, len(all_records),
            )

            if self.on_progress:
                self.on_progress(chunk_num, total_chunks, len(all_records))

            chunk_start = chunk_end

        _logger.info("King County %s complete — %d records with parcel IDs", self.DOC_TYPE_LABEL, len(all_records))
        return all_records

    # ─── Disclaimer ──────────────────────────────────────────────────────────

    async def _accept_disclaimer(self) -> None:
        """Accept the LandmarkWeb disclaimer if present."""
        try:
            await self.page.wait_for_timeout(2000)

            # Method 1: Call SetDisclaimer() JS directly (works for Clark + hidden modals)
            has_fn = await self.page.evaluate("typeof SetDisclaimer === 'function'")
            if has_fn:
                _logger.info("Disclaimer — calling SetDisclaimer() via JS")
                await self.page.evaluate("SetDisclaimer()")
                await self.page.wait_for_timeout(3000)
                _logger.info("Disclaimer accepted via JS")
                return

            # Method 2: Click the Accept button (King County style)
            accept_btn = self.page.locator(
                "button:has-text('Accept'), a:has-text('Accept'), "
                "#btnDisclaimerAccept, "
                "a:has-text('I Accept'), a:has-text('Agree')"
            )
            if await accept_btn.count() > 0:
                _logger.info("Disclaimer found — clicking Accept")
                try:
                    async with self.page.expect_navigation(timeout=10_000):
                        await accept_btn.first.click()
                except Exception:
                    await accept_btn.first.click()
                    await self.page.wait_for_timeout(3000)
                _logger.info("Disclaimer accepted")
            else:
                _logger.info("No disclaimer — already accepted")
            await self.page.wait_for_timeout(1000)
        except Exception as exc:
            _logger.info("Disclaimer: %s", str(exc)[:80])

    # ─── CAPTCHA handling ────────────────────────────────────────────────────

    async def _solve_captcha_once(self) -> None:
        """Handle reCAPTCHA — auto-solve via 2Captcha or wait for manual solve.

        Priority:
        1. If CAPTCHA_ENABLED + CAPTCHA_API_KEY set → use 2Captcha service
        2. Otherwise → wait for user to solve manually in headed mode

        The reCAPTCHA stays solved for the entire browser session,
        so we only need to solve it once.
        """
        # Navigate to Document Type Search to make the captcha visible
        await self._go_to_doc_type_search()
        await self.page.wait_for_timeout(1000)

        # Check if reCAPTCHA is present
        recaptcha = self.page.locator(
            "iframe[src*='recaptcha'], .g-recaptcha, [data-sitekey]"
        )
        if await recaptcha.count() == 0:
            _logger.info("No reCAPTCHA found — proceeding")
            return

        # Extract the sitekey from the page
        sitekey = await self.page.evaluate("""
            (() => {
                const el = document.querySelector('[data-sitekey]');
                return el ? el.getAttribute('data-sitekey') : null;
            })()
        """)
        _logger.info("reCAPTCHA detected (sitekey: %s)", sitekey[:20] if sitekey else "unknown")

        # Method 1: Auto-solve via 2Captcha if API key is available
        from src.config import settings
        has_captcha_key = bool(settings.CAPTCHA_API_KEY)
        if has_captcha_key and sitekey:
            _logger.info("Solving reCAPTCHA via 2Captcha service...")
            try:
                from src.scrapers.enrichment.captcha import solve_recaptcha
                token = await solve_recaptcha(self._base_url, sitekey)
                if token:
                    # Inject the token by:
                    # 1. Setting the textarea value
                    # 2. Monkey-patching grecaptcha.getResponse() to return our token
                    # LandmarkWeb reads the token via grecaptcha.getResponse() in its
                    # AJAX submit — setting the textarea alone doesn't work.
                    await self.page.evaluate("""
                        (token) => {
                            // Set all reCAPTCHA response textareas
                            document.querySelectorAll(
                                '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
                            ).forEach(ta => { ta.value = token; });

                            // Monkey-patch grecaptcha.getResponse to return our token
                            if (typeof grecaptcha !== 'undefined') {
                                grecaptcha.getResponse = () => token;
                            }
                        }
                    """, token)
                    _logger.info("reCAPTCHA token injected + getResponse patched")
                    await self.page.wait_for_timeout(1000)
                    return
                else:
                    _logger.warning("2Captcha failed — falling back to manual solve")
            except Exception as exc:
                _logger.warning("2Captcha error: %s — falling back to manual", str(exc)[:80])

        # Method 2: Wait for user to solve manually (headed mode)
        _logger.info("=" * 60)
        _logger.info("reCAPTCHA — PLEASE SOLVE IT IN THE BROWSER")
        _logger.info("Click 'I'm not a robot' and complete the challenge")
        _logger.info("Waiting up to 5 minutes...")
        _logger.info("=" * 60)

        solved = False
        for _ in range(150):  # 150 * 2s = 5 minutes
            try:
                is_solved = await self.page.evaluate("""
                    (() => {
                        const ta = document.querySelector(
                            '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
                        );
                        return ta && ta.value && ta.value.length > 20;
                    })()
                """)
                if is_solved:
                    solved = True
                    _logger.info("reCAPTCHA solved!")
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        if not solved:
            _logger.warning("reCAPTCHA was not solved within 5 minutes")

        await self.page.wait_for_timeout(1000)

    # ─── Search flow ─────────────────────────────────────────────────────────

    async def _search_chunk(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Navigate to Document Type Search, select document type, fill dates, submit."""
        await self._go_to_doc_type_search()
        await self._select_document_type()
        await self._fill_dates(date_from, date_to)
        await self._submit_search()
        return await self._extract_all_pages()

    async def _go_to_doc_type_search(self) -> None:
        """Click Document Type Search in the left sidebar."""
        try:
            doc_type_link = self.page.locator(
                "#searchCriteriaDocuments-tab, "
                "a:has-text('Document Type Search')"
            )
            if await doc_type_link.count() > 0:
                await doc_type_link.first.click()
                await self.page.wait_for_timeout(1500)
                _logger.info("Clicked Document Type Search tab")
                return
        except Exception:
            pass

        # Fallback: navigate directly then re-accept disclaimer
        url = f"{self._base_url}/search/index?theme=.blue&section=searchCriteriaDocuments"
        if "/search/" in self._base_url:
            url = f"{self._base_url}?theme=.blue&section=searchCriteriaDocuments"
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self.page.wait_for_timeout(2000)
        await self._accept_disclaimer()
        # After disclaimer, try clicking the tab again
        try:
            tab = self.page.locator("#searchCriteriaDocuments-tab, a:has-text('Document Type')")
            if await tab.count() > 0:
                await tab.first.click()
                await self.page.wait_for_timeout(1500)
        except Exception:
            pass
        _logger.info("Navigated to Document Type Search via URL")

    async def _select_document_type(self) -> None:
        """Select document type from Document Category dropdown (#documentCategory-DocumentType).

        Uses jQuery Select2 API since the dropdown is a Select2 widget.
        Matches against DOC_TYPE_SEARCH_TEXTS (case-insensitive substring match).
        If multiple search texts are provided, selects the first match found.
        """
        await self.page.wait_for_timeout(500)

        search_texts = [t.lower() for t in self.DOC_TYPE_SEARCH_TEXTS]

        result = await self.page.evaluate("""
            ((searchTexts) => {
                const sel = document.querySelector('#documentCategory-DocumentType');
                if (!sel) return {success: false, error: 'documentCategory-DocumentType not found'};

                // Find first option matching any of our search texts
                for (const opt of sel.options) {
                    const optText = opt.text.toLowerCase();
                    for (const search of searchTexts) {
                        if (optText.includes(search)) {
                            // Use Select2 API if available (required for form to recognize selection)
                            if (window.jQuery && jQuery.fn.select2) {
                                jQuery('#documentCategory-DocumentType').val(opt.value).trigger('change');
                            } else {
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                            return {success: true, value: opt.value, text: opt.text.trim()};
                        }
                    }
                }
                const opts = Array.from(sel.options).map(o => o.text.trim());
                return {success: false, error: 'No matching document type', searchTexts: searchTexts, opts: opts};
            })
        """, search_texts)

        if result.get('success'):
            _logger.info("Selected '%s' (value=%s)", result.get('text'), result.get('value'))
        else:
            _logger.warning("Could not select %s: %s", self.DOC_TYPE_LABEL, result.get('error'))
            _logger.info("Searched for: %s", result.get('searchTexts', []))
            _logger.info("Available options: %s", result.get('opts', []))

        await self.page.wait_for_timeout(500)

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the begin/end date fields in the DocumentType section."""
        try:
            begin_el = self.page.locator("#beginDate-DocumentType")
            end_el = self.page.locator("#endDate-DocumentType")

            if await begin_el.count() > 0 and await end_el.count() > 0:
                await begin_el.first.click()
                await begin_el.first.fill("")
                await begin_el.first.press_sequentially(date_from, delay=30)
                await begin_el.first.press("Tab")

                await end_el.first.click()
                await end_el.first.fill("")
                await end_el.first.press_sequentially(date_to, delay=30)
                await end_el.first.press("Tab")

                _logger.info("Dates filled: %s to %s", date_from, date_to)
            else:
                _logger.warning("Date inputs #beginDate-DocumentType / #endDate-DocumentType not found")

            await self.page.wait_for_timeout(500)
        except Exception as exc:
            _logger.warning("Could not set dates: %s", str(exc)[:120])

    async def _ensure_captcha_token(self) -> None:
        """Ensure a valid reCAPTCHA token is available for the next submit.

        Solves via 2Captcha if key is available, then patches grecaptcha.getResponse().
        Must be called RIGHT BEFORE submit so the token is fresh.
        """
        from src.config import settings
        if not settings.CAPTCHA_API_KEY:
            return  # No auto-solve available

        # Check if captcha is on this page
        has_captcha = await self.page.evaluate("""
            () => !!document.querySelector('[data-sitekey], .g-recaptcha, iframe[src*="recaptcha"]')
        """)
        if not has_captcha:
            return

        sitekey = await self.page.evaluate("""
            () => {
                const el = document.querySelector('[data-sitekey]');
                return el ? el.getAttribute('data-sitekey') : null;
            }
        """)
        if not sitekey:
            return

        _logger.info("Solving reCAPTCHA before submit...")
        try:
            from src.scrapers.enrichment.captcha import solve_recaptcha
            token = await solve_recaptcha(self._base_url, sitekey)
            if token:
                await self.page.evaluate("""
                    (token) => {
                        // Set textarea
                        document.querySelectorAll(
                            '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
                        ).forEach(ta => { ta.value = token; });
                        // Patch getResponse so the AJAX submit reads our token
                        if (typeof grecaptcha !== 'undefined') {
                            grecaptcha.getResponse = () => token;
                        }
                    }
                """, token)
                _logger.info("reCAPTCHA token ready for submit")
            else:
                _logger.warning("2Captcha failed to solve")
        except Exception as exc:
            _logger.warning("Captcha solve error: %s", str(exc)[:80])

    async def _submit_search(self) -> None:
        """Solve captcha + click Submit + wait for results."""
        try:
            submit_btn = self.page.locator("#submit-DocumentType")
            if await submit_btn.count() == 0:
                _logger.warning("Submit button #submit-DocumentType not found")
                return

            # Solve captcha RIGHT BEFORE clicking submit (token is freshest here)
            await self._ensure_captcha_token()

            await submit_btn.first.scroll_into_view_if_needed()
            await self.page.wait_for_timeout(300)
            await submit_btn.first.click()
            _logger.info("Submit clicked")

            # Wait for AJAX results to load (spinner appears then disappears)
            try:
                await self.page.wait_for_function(
                    """() => {
                        const sr = document.querySelector('#searchResults');
                        if (!sr) return false;
                        const html = sr.innerHTML;
                        // Still loading if spinner/loader is present
                        if (html.includes('ajax-loader') || html.includes('LOADING')) return false;
                        // Done when: has a table, or has meaningful content
                        return html.includes('<table') ||
                               html.toLowerCase().includes('no results') ||
                               html.toLowerCase().includes('no records') ||
                               html.toLowerCase().includes('invalid captcha');
                    }""",
                    timeout=60_000,
                )
            except Exception:
                await self.page.wait_for_timeout(10_000)

            # Check for captcha error
            captcha_error = await self.page.evaluate("""
                (() => {
                    const body = document.body.innerText || '';
                    return body.includes('Invalid Captcha') || body.includes('invalid captcha');
                })()
            """)
            if captcha_error:
                _logger.warning("Invalid Captcha — solving fresh token and retrying...")
                # Invalidate cached token and solve fresh
                from src.scrapers.enrichment.captcha import invalidate_token
                invalidate_token(await self.page.evaluate(
                    "() => document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey') || ''"
                ))
                await self._ensure_captcha_token()
                await submit_btn.first.click()
                _logger.info("Retrying submit after captcha...")
                try:
                    await self.page.wait_for_function(
                        """() => {
                            const sr = document.querySelector('#searchResults');
                            if (!sr) return false;
                            const html = sr.innerHTML;
                            if (html.includes('ajax-loader') || html.includes('LOADING')) return false;
                            return html.includes('<table') ||
                                   html.toLowerCase().includes('no results');
                        }""",
                        timeout=60_000,
                    )
                except Exception:
                    await self.page.wait_for_timeout(10_000)

            _logger.info("Results page ready")
            await self.page.wait_for_timeout(2000)

        except Exception as exc:
            _logger.warning("Submit error: %s", str(exc)[:120])

    # ─── Extraction ──────────────────────────────────────────────────────────

    async def _extract_all_pages(self) -> list[ScrapedRecord]:
        """Extract records from all result pages, filtering for PID."""
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1

            records = await self._extract_page()
            new_count = 0
            for record in records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info("Page %d — %d records with PID (total: %d)", page_num, new_count, len(all_records))

            if new_count == 0 and page_num > 1:
                break

            has_next = await self._go_next_page()
            if not has_next:
                break

        return all_records

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract death certificate records from current results page.

        LandmarkWeb results table columns:
        Recording #, Record Date, Doc Type, Grantor, Grantee, Legal
        """
        records: list[ScrapedRecord] = []

        try:
            # LandmarkWeb uses DataTables (#resultsTable) with many header columns.
            # Build a header-to-index map dynamically, then extract each row.
            raw = await self.page.evaluate("""
                (() => {
                    const table = document.querySelector('#resultsTable, table.dataTable');
                    if (!table) return {error: 'no table', html: document.querySelector('#searchResults')?.innerHTML?.substring(0, 300) || ''};

                    // DataTable rows have 24 visible <td> cells.
                    // The <th> headers have hidden columns so we can't map by index.
                    // Use the ACTUAL cell positions from inspection:
                    //   0: row#, 3: status, 5: grantor, 6: grantee, 7: date,
                    //   8: doc_type, 12: rec#, 14: legal
                    // Column positions vary by county (King=24 cols, Clark=25 cols).
                    // Use flexible extraction: find PID in ANY cell, map known positions.
                    const COL = {grantor: 5, grantee: 6, date: 7, docType: 8, recNum: 12};

                    const rows = table.querySelectorAll('tbody tr');
                    const results = [];

                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 8) continue;

                        const get = (idx) => cells[idx] ? cells[idx].textContent.trim() : '';

                        const grantor = get(COL.grantor);
                        const grantee = get(COL.grantee);
                        const dateStr = get(COL.date);
                        const docType = get(COL.docType);
                        const recNum = get(COL.recNum);
                        // Search ALL cells for legal/PID (column varies by county)
                        let legal = '';
                        for (let i = 10; i < cells.length; i++) {
                            const txt = cells[i].textContent.trim();
                            if (txt.includes('PID') || txt.includes('SUB:') || txt.includes('LOT') || txt.includes('SEC')) {
                                legal = txt;
                                break;
                            }
                        }

                        // Skip rows without meaningful data
                        if (!grantor && !dateStr) continue;

                        results.push({
                            instrument: recNum,
                            grantor: grantor,
                            grantee: grantee,
                            date_recorded: dateStr,
                            doc_type: docType,
                            legal: legal,
                        });
                    }

                    return {
                        data: results,
                        totalRows: rows.length,
                    };
                })()
            """)

            # Handle the response — could be error dict or data dict
            if isinstance(raw, dict) and 'error' in raw:
                _logger.info("No results table: %s — html: %s", raw.get('error'), raw.get('html', '')[:200])
                return []

            if isinstance(raw, dict):
                header_map = raw.get('headerMap', {})
                total_rows = raw.get('totalRows', 0)
                data = raw.get('data', [])
                _logger.info("DataTable: %d total rows, %d data rows, headerMap=%s",
                             total_rows, len(data), header_map)
                raw = data
            else:
                raw = raw or []

            if not raw:
                _logger.info("No results found")
                return []

            _logger.info("Data rows: %d", len(raw))

            # Log first 3 rows
            for i, sample in enumerate(raw[:3]):
                if sample:
                    _logger.info("  Row %d: %s", i + 1,
                                 {k: (v[:80] if isinstance(v, str) and v else v) for k, v in sample.items()})

            for item in raw:
                if not item:
                    continue

                legal = (item.get("legal") or "").strip()

                # Extract parcel ID from legal description
                # Formats seen: "PID:1234567890", "PID 1234567890", just digits
                pid_match = _PID_PATTERN.search(legal)
                if not pid_match:
                    # Skip records without a parcel ID
                    continue

                parcel_id = pid_match.group(1)

                record = ScrapedRecord()
                record.parcel_id = parcel_id

                # Recording number
                inst = (item.get("instrument") or "").strip()
                if inst:
                    record.legal_description = inst

                # Recording date
                date_str = (item.get("date_recorded") or "").strip()
                if date_str:
                    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                    if date_match:
                        record.date_recorded = date_match.group(1)

                # Grantor = deceased person
                grantor = (item.get("grantor") or "").strip()
                if grantor:
                    record.party_name = grantor

                # Grantee = heir/family inheriting the property
                grantee = (item.get("grantee") or "").strip()
                if grantee:
                    record.heirs = grantee

                # Doc type
                doc_type = (item.get("doc_type") or "").strip()
                record.doc_type = doc_type or self.DOC_TYPE_LABEL

                # Store all metadata in enrichment_data
                record.enrichment_data = {
                    "source": "king_county_recorder",
                    "recording_number": inst,
                    "parcel_id": parcel_id,
                    "legal_description": legal,
                    "doc_type": doc_type,
                }

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Records with PID: %d / %d total", len(records), len(raw))

        except Exception as exc:
            _logger.warning("Extract error: %s", str(exc)[:120])

        return records

    async def _go_next_page(self) -> bool:
        """Click the Next page button in LandmarkWeb pagination."""
        try:
            next_btn = self.page.locator(
                "a:has-text('Next'), button:has-text('Next'), "
                "a[title*='Next'], .pagination .next a, "
                "[aria-label='Next']"
            )
            if await next_btn.count() > 0:
                first = next_btn.first
                disabled = await first.get_attribute("disabled")
                cls = await first.get_attribute("class") or ""
                if disabled or "disabled" in cls.lower():
                    return False

                await first.click()
                await self.page.wait_for_timeout(4000)
                return True
        except Exception:
            pass
        return False


# ─── Backward-compatible aliases ─────────────────────────────────────────────
# Old code may reference these class names — they all resolve to the base class
# which now accepts record_type in constructor.
LandmarkWebDeathCertScraper = KingCountyLandmarkWebScraper
KingWaProbateScraper = KingCountyLandmarkWebScraper
KingWaPreForeclosureScraper = KingCountyLandmarkWebScraper
KingWaDivorceScraper = KingCountyLandmarkWebScraper


# ─── Other LandmarkWeb counties ─────────────────────────────────────────────

class ClarkWaProbateScraper(KingCountyLandmarkWebScraper):
    """Clark County, WA — e-docs.clark.wa.gov/LandmarkWeb

    Clark's Document Type dropdown doesn't have specific categories like
    'Death Certificate'. Uses 'Main Dump for Digital Archives' (all docs)
    and relies on PID filtering to get property-related records.
    """

    # Override: Clark uses "All Categories" from dropdown (gets everything with PID)
    RECORD_TYPE_CONFIG = {
        **KingCountyLandmarkWebScraper.RECORD_TYPE_CONFIG,
        "probate": {
            "search_texts": ["all categories"],
            "label": "ALL RECORDS",
            "grantor": "grantor",
            "grantee": "grantee",
        },
        "pre_foreclosure": {
            "search_texts": ["all categories"],
            "label": "ALL RECORDS",
            "grantor": "borrower",
            "grantee": "lender",
        },
    }

    def __init__(self, record_type: str = "probate"):
        super().__init__(
            base_url="https://e-docs.clark.wa.gov/LandmarkWeb",
            county="clark", state="WA", record_type=record_type,
        )


class SnohomishWaProbateScraper(KingCountyLandmarkWebScraper):
    """Snohomish County, WA — snoco.org/RecordedDocuments (requires login — NOT PUBLIC)"""
    def __init__(self, record_type: str = "probate"):
        super().__init__(
            base_url="https://www.snoco.org/RecordedDocuments",
            county="snohomish", state="WA",
        )
