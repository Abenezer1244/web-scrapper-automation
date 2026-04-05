"""King County (WA) Recorder — Death Certificate scraper via LandmarkWeb.

Portal: https://recordsearch.kingcounty.gov/LandmarkWeb/search/index
Platform: Hyland LandmarkWeb

Flow:
1. Accept disclaimer (if present)
2. Click "Document Type Search" in left sidebar
3. Select "Death Certificates" from Document Category dropdown (#documentCategory-DocumentType)
4. Fill date range (#beginDate-DocumentType / #endDate-DocumentType)
5. Solve reCAPTCHA (manual in headed mode, or automated via service)
6. Click Submit (#submit-DocumentType)
7. Extract results: Recording #, Date, Grantor (deceased), Grantee (heir), Legal
8. Filter for records with PID (Parcel ID) in the Legal column

Key:
- Grantor = deceased person
- Grantee = heir/family member inheriting the property (the lead)
- PID:####### in Legal column = property parcel ID

Enrichment (Phase 2): eRealProperty for property + mailing addresses.
"""

import asyncio
import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_wa_probate")

_BASE_URL = "https://recordsearch.kingcounty.gov"
_SEARCH_URL = f"{_BASE_URL}/LandmarkWeb/search/index"

# Regex to extract parcel ID from legal description (e.g. "PID:1234567890")
_PID_PATTERN = re.compile(r"PID[:\s]*(\d{6,12})", re.IGNORECASE)


class LandmarkWebDeathCertScraper(BridgeScraper):
    """Scrapes death certificate filings from any LandmarkWeb Recorder portal.

    Works with: King, Clark, Snohomish (WA) — all use Hyland LandmarkWeb.

    Death certificates with parcel IDs indicate property ownership by the deceased.
    Grantor = deceased, Grantee = heir/family inheriting the property.
    """

    def __init__(self, base_url: str | None = None, county: str = "king", state: str = "WA"):
        super().__init__()
        self._base_url = (base_url or _BASE_URL).rstrip("/")
        self._county = county
        self._state = state

        from urllib.parse import urlparse
        domain = urlparse(self._base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 30

        total_chunks = max(1, (end - start).days // chunk_days + 1)

        _logger.info(
            "%s County death certs — %s to %s (%d chunks of %d days)",
            self._county.title(), date_from, date_to, total_chunks, chunk_days,
        )

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        chunk_num = 0

        # Navigate and accept disclaimer once
        search_url = f"{self._base_url}/search/index" if "/search/" not in self._base_url else self._base_url
        await self.navigate(search_url)
        await self._accept_disclaimer()

        # Solve CAPTCHA once at the start (user does it manually in headed mode)
        await self._solve_captcha_once()

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

        _logger.info("King County death certs complete — %d records with parcel IDs", len(all_records))
        return all_records

    # ─── Disclaimer ──────────────────────────────────────────────────────────

    async def _accept_disclaimer(self) -> None:
        """Accept the LandmarkWeb disclaimer if present."""
        try:
            await self.page.wait_for_timeout(2000)
            accept_btn = self.page.locator(
                "button:has-text('Accept'), a:has-text('Accept'), "
                "#btnDisclaimerAccept, [onclick*='SetDisclaimer'], "
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

        # Method 1: Auto-solve via 2Captcha if available
        from src.config import settings
        if settings.CAPTCHA_ENABLED and settings.CAPTCHA_API_KEY and sitekey:
            _logger.info("Solving reCAPTCHA via 2Captcha service...")
            try:
                from src.scrapers.enrichment.captcha import solve_recaptcha
                token = await solve_recaptcha(self._base_url, sitekey)
                if token:
                    # Inject the solved token into the page
                    await self.page.evaluate(f"""
                        (() => {{
                            const textarea = document.querySelector(
                                '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
                            );
                            if (textarea) {{
                                textarea.value = '{token}';
                                textarea.dispatchEvent(new Event('change', {{bubbles: true}}));
                            }}
                            // Also set via callback if available
                            if (typeof grecaptcha !== 'undefined') {{
                                try {{ grecaptcha.enterprise?.execute?.(); }} catch(e) {{}}
                            }}
                        }})()
                    """)
                    _logger.info("reCAPTCHA token injected via 2Captcha")
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
        """Navigate to Document Type Search, select Death Certificate, fill dates, submit."""
        await self._go_to_doc_type_search()
        await self._select_death_certificate()
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

        # Fallback: navigate directly
        url = f"{self._base_url}/search/index?theme=.blue&section=searchCriteriaDocuments"
        if "/search/" in self._base_url:
            url = f"{self._base_url}?theme=.blue&section=searchCriteriaDocuments"
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self.page.wait_for_timeout(2000)
        _logger.info("Navigated to Document Type Search via URL")

    async def _select_death_certificate(self) -> None:
        """Select Death Certificates from Document Category (#documentCategory-DocumentType).

        Uses jQuery Select2 API since the dropdown is a Select2 widget.
        """
        await self.page.wait_for_timeout(500)

        result = await self.page.evaluate("""
            (() => {
                const sel = document.querySelector('#documentCategory-DocumentType');
                if (!sel) return {success: false, error: 'documentCategory-DocumentType not found'};

                // Find Death Certificate option
                for (const opt of sel.options) {
                    if (opt.text.toLowerCase().includes('death cert')) {
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
                const opts = Array.from(sel.options).slice(0, 10).map(o => o.text.trim());
                return {success: false, error: 'Death Certificate not in options', opts: opts};
            })()
        """)

        if result.get('success'):
            _logger.info("Selected '%s' (value=%s)", result.get('text'), result.get('value'))
        else:
            _logger.warning("Could not select Death Certificate: %s", result.get('error'))
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

    async def _submit_search(self) -> None:
        """Click the DocumentType Submit button and wait for results to load."""
        try:
            submit_btn = self.page.locator("#submit-DocumentType")
            if await submit_btn.count() == 0:
                _logger.warning("Submit button #submit-DocumentType not found")
                return

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
                _logger.warning("Invalid Captcha error — waiting for user to solve reCAPTCHA...")
                await self._solve_captcha_once()
                # Retry submit after captcha is solved
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
                    const COL = {grantor: 5, grantee: 6, date: 7, docType: 8, recNum: 12, legal: 14};

                    const rows = table.querySelectorAll('tbody tr');
                    const results = [];

                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 15) continue;  // Need at least 15 cells for legal column

                        const get = (idx) => cells[idx] ? cells[idx].textContent.trim() : '';

                        const grantor = get(COL.grantor);
                        const grantee = get(COL.grantee);
                        const dateStr = get(COL.date);
                        const docType = get(COL.docType);
                        const recNum = get(COL.recNum);
                        const legal = get(COL.legal);

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
                record.doc_type = doc_type or "DEATH CERTIFICATE"

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


# ─── County-specific aliases ─────────────────────────────────────────────────
# Each alias pre-configures the base URL for a specific LandmarkWeb county.
# The registry uses these class names in county_connectors.scraper_class.

class KingWaProbateScraper(LandmarkWebDeathCertScraper):
    """King County, WA — recordsearch.kingcounty.gov"""
    def __init__(self):
        super().__init__(
            base_url="https://recordsearch.kingcounty.gov/LandmarkWeb",
            county="king", state="WA",
        )


class ClarkWaProbateScraper(LandmarkWebDeathCertScraper):
    """Clark County, WA — e-docs.clark.wa.gov/LandmarkWeb"""
    def __init__(self):
        super().__init__(
            base_url="https://e-docs.clark.wa.gov/LandmarkWeb",
            county="clark", state="WA",
        )


class SnohomishWaProbateScraper(LandmarkWebDeathCertScraper):
    """Snohomish County, WA — snoco.org/RecordedDocuments (requires login — NOT PUBLIC)"""
    def __init__(self):
        super().__init__(
            base_url="https://www.snoco.org/RecordedDocuments",
            county="snohomish", state="WA",
        )
