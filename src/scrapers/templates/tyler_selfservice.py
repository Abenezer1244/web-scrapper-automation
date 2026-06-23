"""Tyler SelfService template scraper for `*.tylerhost.net/Web` recorder portals.

Covers Tyler SelfService Web installations used by several WA counties:
Grant, Okanogan, Lincoln, Stevens (initially — potentially more).

This platform is DIFFERENT from Tyler EagleWeb despite sharing a vendor:
- Disclaimer page: `/Web/user/disclaimer` with `#submitDisclaimerAccept` that
  is `disabled=true` on load. A client-side script is supposed to enable it
  on user interaction; in headless Playwright the trigger never fires so we
  force-enable it via JS before clicking.
- Search flow goes via Official Records Search → Document Type Search at
  `/Web/search/DOCSEARCH{ACTIONGROUP}S3`. The ACTIONGROUP id varies per
  county (e.g. okanogan: 769, grant: TBD). We discover it by following the
  "Official Records Search" link on the home page.
- Search form fields are fixed across counties:
  - `#field_RecDateID_DOT_StartDate` (mm/dd/yyyy)
  - `#field_RecDateID_DOT_EndDate` (mm/dd/yyyy)
  - `#field_selfservice_documentTypes` (autocomplete, left empty so we search
    all types and filter client-side)
  - Submit via `#searchButton` (an `<a>`, not a `<button>`)
- Results render via AJAX into `li.ss-search-row` elements — 100 per page.
  Each row has `data-documentid="DOC{...}"` and `data-href="/Web/document/..."`.
- Parcel IDs do NOT appear in the search result rows. They live on the
  detail page, which requires additional server-side session state we
  don't yet know how to establish programmatically. This first version
  therefore extracts metadata only (no parcel), relying on
  require_parcel_id=False so records survive. Detail-page parcel
  enrichment is TODO.

Volume reference (okanogan 2026-04-11):
- 30-day total docs: ~537 (all types)
- 30-day probate-relevant: ~9 (Death Cert, TOD Deed, Personal Rep Deed, Will, CPA)
- 30-day pre_foreclosure-relevant: ~2 (Notice of Trustee's Sale, Lis Pendens)
- 90-day probate-relevant: ~63
"""

import re

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.divorce import is_divorce_doc, orient_divorce_party
from src.scrapers.preforeclosure import (
    is_cancellation_or_admin,
    orient_pre_foreclosure_party,
)
from src.scrapers.probate import orient_probate_party
from src.scrapers.reliability import (
    ScraperBlockedError,
    ScraperExecutionError,
    classify_results_page,
    detect_block,
)
from src.utils.logger import setup_logger

# Substrings a Tyler SelfService results page renders for a genuine zero-result
# window (it has no numeric results header). Kept exact to avoid misreading a
# block/login page as a genuine empty.
_EMPTY_MARKERS = ("no records", "no results", "0 records", "did not return any", "no documents found")

_logger = setup_logger("scraper.template.tyler_selfservice")

# Keywords that mark a row as matching each record type. Tyler SelfService
# doc type labels vary slightly from EagleWeb (e.g. "Personal Representative's
# Deed" has the apostrophe, "Notice Of Trustee's Sale" is capitalised
# differently). We match case-insensitively so style differences don't matter.
_DOC_TYPE_MAP = {
    "probate": [
        "PROBATE",
        "LETTERS TESTAMENTARY",
        "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE",
        "DEATH CERTIFICATE",
        "AFFIDAVIT OF HEIRSHIP",
        "TRANSFER ON DEATH",
        "COMMUNITY PROPERTY AGREEMENT",
        "WILL",
    ],
    "pre_foreclosure": [
        "LIS PENDENS",
        "NOTICE OF TRUSTEE",
        "TRUSTEE'S SALE",
        "NOTICE OF DEFAULT",
        "FORECLOSURE",
        "NOTICE OF INTENT TO FORFEIT",
    ],
    "tax_delinquent": [
        "TAX LIEN",
        "CERTIFICATE OF DELINQUENCY",
        "TREASURER'S DEED",
    ],
    "divorce": [
        "DIVORCE",
        "DISSOLUTION",
        "DECREE OF DISSOLUTION",
    ],
}


class TylerSelfServiceScraper(BridgeScraper):
    """Template scraper for Tyler SelfService Web recorder installations.

    Zero Claude AI cost — uses standardized selectors for the shared
    Tyler SelfService interface.

    WARNING (2026-04-11): this version does NOT extract parcel_id — the
    search-result rows don't contain it and the detail page requires
    server-side session state we can't yet replicate. Instantiate with
    require_parcel_id=False to keep records.
    """

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor derived from this template's own _DOC_TYPE_MAP."""
        from src.scrapers.doc_scope import from_keyword_map

        return from_keyword_map(_DOC_TYPE_MAP, record_type)

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        require_parcel_id: bool = False,
    ):
        super().__init__()
        # Normalise base_url so the rest of the template can build sub-paths
        # predictably. Connectors in the DB store a variety of URLs for the
        # same platform — some point to the root /Web, some to /web/ with
        # lowercase, some to /web/user/disclaimer (the first page a user
        # lands on). We normalize ALL of them to `https://{host}/Web`, the
        # canonical entry point, by stripping any path suffix and always
        # re-appending /Web. This also avoids the "/Web/Web/" double-prefix
        # bug and the case-sensitivity mismatch between /Web and /web.
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        if not host:
            # Tolerate callers that passed a path-only URL
            raise ValueError(f"Tyler SelfService base_url has no host: {base_url!r}")
        self.web_root = f"{scheme}://{host}/Web"
        # base_url is the entry URL we navigate to; Tyler SelfService will
        # immediately redirect to /Web/user/disclaimer. Both /Web and /web
        # resolve to the same place on every installation we've seen.
        self.base_url = self.web_root
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        # Tyler SelfService doesn't expose parcel IDs in search results yet,
        # so callers must opt INTO parcel dropping. Default is False, which
        # is the opposite of EagleWebScraper.
        self.require_parcel_id = require_parcel_id

        from urllib.parse import urlparse
        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape Tyler SelfService records for the given date range.

        Flow:
        1. Navigate to base_url, get redirected to /Web/user/disclaimer
        2. Force-enable #submitDisclaimerAccept via JS (the client-side
           enable script doesn't fire in headless) and click
        3. Navigate to /Web/ home, find "Official Records Search" link and
           follow it to discover the ACTIONGROUP id
        4. Navigate to /Web/search/DOCSEARCH{actionGroup}S3 (Document Type Search)
        5. Fill date range fields, click #searchButton
        6. Wait for AJAX response — results render into li.ss-search-row
        7. Extract metadata from each row
        8. Paginate by clicking "next" until all pages consumed
        9. Filter by self.active_record_type keywords client-side
        """
        _logger.info(
            "Tyler SelfService scraper — %s/%s — %s to %s",
            self.county, self.state, date_from, date_to,
        )

        # Some Tyler SelfService installations mount at /Web (okanogan) and
        # others at /web (lincoln, stevens). Try the canonical /Web first,
        # fall back to /web if we get a 404 / "Not Found" title.
        await self.navigate(self.base_url)
        title = (await self.page.title()) or ""
        if "Not Found" in title or "404" in title:
            lowercase_root = self.web_root[:-4] + "/web"  # swap "/Web" -> "/web"
            _logger.info("/Web returned 404, retrying %s", lowercase_root)
            self.web_root = lowercase_root
            self.base_url = lowercase_root
            await self.navigate(lowercase_root)

        # Step 1: disclaimer (a required disclaimer we cannot accept = real failure)
        if not await self._bypass_disclaimer():
            raise ScraperExecutionError(
                self.county, "Tyler SelfService",
                "disclaimer bypass failed (search never started)",
                record_type=self.active_record_type,
            )

        # Step 2: find the Official Records Search action group id by walking
        # the home page. Tyler names links like "Official Records Search" —
        # the href points to /Web/action/ACTIONGROUP{N}S1 where N is the
        # county-specific group id. We then swap ACTIONGROUP...S1 for
        # DOCSEARCH...S3 (Document Type Search).
        search_url = await self._discover_search_url()
        if not search_url:
            raise ScraperExecutionError(
                self.county, "Tyler SelfService",
                "could not discover Document Type Search URL (page layout changed / blocked)",
                record_type=self.active_record_type,
            )

        _logger.info("Document Type Search URL: %s", search_url)
        await self.navigate(search_url)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.page.wait_for_timeout(2_000)

        # Step 3: fill dates and submit
        if not await self._fill_and_submit(date_from, date_to):
            raise ScraperExecutionError(
                self.county, "Tyler SelfService",
                "date-fill / submit failed (search never ran)",
                record_type=self.active_record_type, date_from=date_from, date_to=date_to,
            )

        # Step 4: wait for results to render. A timeout here is NOT automatically
        # "empty" — distinguish a genuine zero-result window (explicit marker) from
        # a block / silent failure (raise). Otherwise a blocked search hands the
        # tenant an empty list under a DONE job.
        try:
            await self.page.wait_for_selector("li.ss-search-row", timeout=15_000)
        except Exception:
            page_text = await self.page.inner_text("body")
            verdict = classify_results_page(
                row_count=0, page_text=page_text, empty_markers=_EMPTY_MARKERS
            )
            if verdict == "block":
                raise ScraperBlockedError(
                    self.county, "Tyler SelfService",
                    f"block wall on results page ({detect_block(page_text)})",
                    record_type=self.active_record_type, context=page_text[:120],
                ) from None
            if verdict == "ambiguous":
                raise ScraperExecutionError(
                    self.county, "Tyler SelfService",
                    "no result rows appeared and no empty-marker (search may have "
                    "silently failed / been blocked)",
                    record_type=self.active_record_type, context=page_text[:120],
                ) from None
            _logger.info("Tyler search returned a genuine empty window")
            return []
        await self.page.wait_for_timeout(2_000)

        # Step 5: paginate + extract
        all_records: list[ScrapedRecord] = []
        seen_ids: set[str] = set()
        page_num = 1
        max_pages = 20  # safety cap — 2000 records for a 30-day window is plenty
        while page_num <= max_pages:
            # Bounded retry on a transient extraction failure, then fail loud.
            page_records = None
            last_exc: ScraperExecutionError | None = None
            for attempt in range(1, 4):
                try:
                    page_records = await self._extract_page(page_num)
                    break
                except ScraperExecutionError as exc:
                    last_exc = exc
                    _logger.warning(
                        "Page %d extract attempt %d/3 failed: %s",
                        page_num, attempt, str(exc)[:100],
                    )
                    if attempt < 3:
                        await self.page.wait_for_timeout(1_500 * attempt)
            if page_records is None:
                raise last_exc or ScraperExecutionError(
                    self.county, "Tyler SelfService",
                    "page extraction failed after retries (no recorded cause)",
                    record_type=self.active_record_type, page=page_num,
                )
            new = 0
            for r in page_records:
                did = r.enrichment_data.get("document_id")
                if did and did in seen_ids:
                    continue
                if did:
                    seen_ids.add(did)
                all_records.append(r)
                new += 1
            _logger.info("Page %d: %d new records (total: %d)", page_num, new, len(all_records))

            if new == 0:
                break

            if not await self._goto_next_page():
                break
            page_num += 1

        # Step 6: filter by active record type
        filtered = self._filter_by_type(all_records)
        _logger.info(
            "Tyler SelfService %s: %d/%d records after %s filter",
            self.county, len(filtered), len(all_records), self.active_record_type or "none",
        )

        # Step 7: enrich parcels via county assessor TaxSifter (name → parcel)
        # Tyler SelfService detail pages are blocked, so we use the county
        # assessor's public search to look up parcels by grantor name.
        needs_parcel = [r for r in filtered if not r.parcel_id and r.party_name]
        if needs_parcel:
            await self._enrich_parcels_via_assessor(needs_parcel)

        # Step 8: drop records without parcel_id if opted in
        if self.require_parcel_id:
            before = len(filtered)
            filtered = [r for r in filtered if r.parcel_id]
            dropped = before - len(filtered)
            if dropped:
                _logger.info("Dropped %d/%d records with no parcel_id", dropped, before)

        return filtered

    async def _bypass_disclaimer(self) -> bool:
        """Force-enable and click the disclaimer Accept button.

        The client-side `$('#submitDisclaimerAccept').prop('disabled', false)`
        is supposed to fire on user interaction but doesn't trigger in
        headless Playwright. We remove the disabled attribute via JS and
        click. This is safe because the disclaimer is purely informational
        — no real consent is recorded beyond a cookie that subsequent
        page loads respect.
        """
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        btn = self.page.locator("#submitDisclaimerAccept").first
        if await btn.count() == 0:
            # Already past disclaimer (cookie set from prior run)
            _logger.info("No disclaimer found — assumed already accepted")
            return True

        await self.page.evaluate(
            "() => { const b = document.getElementById('submitDisclaimerAccept');"
            "  if (b) { b.disabled = false; b.removeAttribute('disabled'); } }"
        )

        try:
            async with self.page.expect_navigation(timeout=15_000, wait_until="domcontentloaded"):
                await btn.click()
        except Exception as exc:
            _logger.warning("Disclaimer click failed: %s", str(exc)[:120])
            return False

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        _logger.info("Disclaimer accepted, now at: %s", self.page.url)
        return True

    async def _discover_search_url(self) -> str | None:
        """Walk from /Web/ home to the Document Type Search URL.

        The Tyler SelfService action group id varies per installation
        (e.g. okanogan: 769). We can't hardcode. Instead we find the
        "Official Records Search" link on the home page, extract the
        ACTIONGROUP number from its href, and construct the Document
        Type Search URL.
        """
        # web_root is "https://host/Web" (no trailing slash), so appending "/"
        # gives the Tyler SelfService home URL exactly.
        home_url = self.web_root + "/"

        # Discover the "Official Records Search" link with retries. After the
        # disclaimer click the URL is already /Web/, but the nav menu that holds
        # this link renders a beat later (and on some loads the /Web/ page briefly
        # re-shows the disclaimer until the session cookie settles). A one-shot
        # query raced that and intermittently found nothing — okanogan returned
        # 0 on ~half of runs. Re-navigate home on each miss and wait for the link
        # element itself to exist before reading its href.
        href = None
        for attempt in range(1, 4):
            if attempt > 1 or self.page.url.split("#")[0] != home_url:
                await self.navigate(home_url)
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                # The /Web/ shell sometimes re-shows the disclaimer until the
                # session cookie settles. Re-accept it (idempotent — a no-op when
                # the menu is already showing) so the nav link can render; without
                # this a reappearing disclaimer makes all retries find no link.
                await self._bypass_disclaimer()

            try:
                await self.page.wait_for_function(
                    """() => Array.from(document.querySelectorAll('a'))
                        .some(a => (a.innerText || '').trim().startsWith('Official Records Search'))""",
                    timeout=10_000,
                )
            except Exception:
                _logger.info(
                    "Official Records Search link not present yet (attempt %d/3)", attempt
                )
                await self.page.wait_for_timeout(2_000)
                continue

            try:
                href = await self.page.evaluate(
                    """() => {
                        const a = Array.from(document.querySelectorAll('a'))
                            .find(a => (a.innerText || '').trim().startsWith('Official Records Search'));
                        return a ? a.getAttribute('href') : null;
                    }"""
                )
            except Exception as exc:
                _logger.warning("JS error finding search link: %s", str(exc)[:120])
                href = None

            if href:
                break
            await self.page.wait_for_timeout(2_000)

        if not href:
            _logger.warning("No 'Official Records Search' link on home page after 3 attempts")
            return None

        # href looks like /Web/action/ACTIONGROUP769S1 — extract 769
        match = re.search(r"ACTIONGROUP(\d+)S", href)
        if not match:
            _logger.warning("Unexpected Official Records Search href: %s", href)
            return None
        action_id = match.group(1)

        # Derive the Document Type Search URL.
        # Path is /Web/search/DOCSEARCH{id}S3 (S3 = Document Type Search,
        # verified on okanogan). web_root already includes the /Web prefix.
        search_url = f"{self.web_root}/search/DOCSEARCH{action_id}S3"
        return search_url

    async def _fill_and_submit(self, date_from: str, date_to: str) -> bool:
        """Fill the date range and click Search.

        Returns True if the AJAX search fired. Detection is by waiting for
        the result row selector afterwards, not by checking this method.
        """
        try:
            await self.page.wait_for_selector("#field_RecDateID_DOT_StartDate", timeout=10_000)
        except Exception:
            _logger.warning("Start date field didn't appear")
            return False

        try:
            await self.page.locator("#field_RecDateID_DOT_StartDate").fill(date_from)
            await self.page.locator("#field_RecDateID_DOT_EndDate").fill(date_to)
            await self.page.wait_for_timeout(500)
        except Exception as exc:
            _logger.warning("Date fill failed: %s", str(exc)[:120])
            return False

        try:
            await self.page.locator("#searchButton").click()
        except Exception as exc:
            _logger.warning("Search button click failed: %s", str(exc)[:120])
            return False

        # Search is AJAX — no navigation. The caller waits for results.
        return True

    async def _extract_page(self, page_num: int = 1) -> list[ScrapedRecord]:
        """Extract all ss-search-row elements on the current page.

        Tyler SelfService renders each record as an <li class="ss-search-row">
        with the following structure:
          - data-documentid: stable document identifier (DOC{...})
          - data-href: detail page URL (requires session context — TODO)
          - <h1>: instrument number + doc type
          - Lists of Recording Date, Grantor, Grantee fields

        Parcel IDs are NOT present at the list level — they're on the
        detail page only. This extraction returns metadata only for now.
        """
        try:
            raw = await self.page.evaluate(
                """() => {
                    const rows = [];
                    document.querySelectorAll('li.ss-search-row').forEach(li => {
                        const doc_id = li.getAttribute('data-documentid') || '';
                        const detail_href = li.getAttribute('data-href') || '';
                        const h1 = li.querySelector('h1');
                        const h1_text = h1 ? h1.innerText.replace(/\\s+/g, ' ').trim() : '';
                        const full_text = li.innerText.replace(/\\r\\n/g, '\\n').trim();
                        rows.push({doc_id, detail_href, h1_text, full_text});
                    });
                    return rows;
                }"""
            )
        except Exception as exc:
            raise ScraperExecutionError(
                self.county, "Tyler SelfService", "page extraction failed",
                page=page_num, record_type=self.active_record_type,
                context=str(exc)[:120],
            ) from exc

        if not raw and page_num == 1:
            # Step 4 already confirmed result rows rendered; extracting 0 here is
            # parse drift (layout change), not a genuine empty window.
            raise ScraperExecutionError(
                self.county, "Tyler SelfService",
                "result rows rendered but extracted 0 (parse drift)",
                page=page_num, record_type=self.active_record_type,
            )

        records: list[ScrapedRecord] = []
        for item in raw:
            doc_id = item.get("doc_id", "")
            h1_text = item.get("h1_text", "")
            full_text = item.get("full_text", "")
            detail_href = item.get("detail_href", "")

            # Parse instrument number + doc type from h1.
            # Format: "3290696   ·   Boundary Adjustment"
            # Some rows show just a number then doc type separated by · or -
            instr_match = re.match(r"^\s*(\d{4,})\s*[\u00B7\-\|]*\s*(.+)$", h1_text)
            if instr_match:
                instrument_number = instr_match.group(1).strip()
                doc_type = instr_match.group(2).strip()
            else:
                instrument_number = ""
                doc_type = h1_text

            # Strip a leading status-icon glyph (e.g. the U+FFFD replacement char
            # some Tyler rows render before the type, "� Notice Of Trustee's
            # Sale") so the stored/classified doc_type starts at the first real
            # letter/digit. Recorder doc types never lead with punctuation.
            if doc_type:
                doc_type = re.sub(r"^[^0-9A-Za-z]+", "", doc_type).strip()

            # Parse "Recording Date" — text looks like
            # "Recording Date\n04/10/2026 02:12 PM"
            date_match = re.search(r"Recording Date\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})", full_text)
            date_recorded = date_match.group(1) if date_match else None

            # Parse Grantor(s) — text looks like
            # "Grantor (2)\nNAME ONE\nNAME TWO"  or  "Grantor\nNAME"
            grantor = None
            gm = re.search(r"Grantor(?:\s*\(\d+\))?\s*\n([^\n]+(?:\n[^\n]+)*?)(?=\nGrantee|\n\n|$)", full_text)
            if gm:
                lines = [ln.strip() for ln in gm.group(1).splitlines() if ln.strip()]
                # Join DISTINCT parties with " / " (the project-wide stacked-party
                # convention) — each line is itself "LAST, FIRST", so a comma can't
                # be the party separator, and " / " lets the pre_foreclosure
                # borrower-orientation split the trustee company off the homeowner.
                grantor = " / ".join(lines[:5]) if lines else None

            # Parse Grantee(s)
            grantee = None
            gem = re.search(r"Grantee(?:\s*\(\d+\))?\s*\n([^\n]+(?:\n[^\n]+)*?)(?=\n\n|$)", full_text)
            if gem:
                lines = [ln.strip() for ln in gem.group(1).splitlines() if ln.strip()]
                grantee = " / ".join(lines[:5]) if lines else None

            record = ScrapedRecord()
            record.date_recorded = date_recorded
            record.doc_type = doc_type or None
            record.party_name = grantor
            record.heirs = grantee  # reuse heirs field for grantee (matches EagleWeb convention)
            record.enrichment_data = {
                "document_id": doc_id,
                "instrument_number": instrument_number,
                "detail_href": detail_href,
                "source": "tyler_selfservice",
            }
            records.append(record)

        return records

    async def _goto_next_page(self) -> bool:
        """Click the 'next' pagination link. Returns False if there is none.

        Waits for the result list to actually SWAP to the next page (the first
        row's document id changes) instead of a bare row-present check. The old
        rows stay in the DOM during the next-page AJAX, so a present-check
        matches the STALE rows instantly; a slow AJAX then let _extract_page run
        on the old page, every row deduped out (new=0), and the caller stopped
        early — silently dropping later pages (okanogan: 300 vs 716 across runs).
        """
        try:
            first_id_before = await self.page.evaluate(
                "() => { const li = document.querySelector('li.ss-search-row');"
                " return li ? li.getAttribute('data-documentid') : null; }"
            )
            # Find all "next" links, pick the first that is both visible and
            # enabled. On the last page, Tyler SelfService renders the Next link
            # with display:none so the locator matches but is_visible() is False.
            next_btns = await self.page.locator(
                "a:has-text('next'), a:has-text('Next')"
            ).all()
            clickable = None
            for btn in next_btns:
                try:
                    if await btn.is_visible() and not await btn.is_disabled():
                        clickable = btn
                        break
                except Exception:
                    continue
            if clickable is None:
                return False
            await clickable.click(timeout=5_000)
            # Wait until the first row's id differs — the next page has rendered.
            try:
                await self.page.wait_for_function(
                    """(prev) => { const li = document.querySelector('li.ss-search-row');
                        return li && li.getAttribute('data-documentid') !== prev; }""",
                    arg=first_id_before,
                    timeout=15_000,
                )
            except Exception:
                # Slow AJAX: give it one more beat and re-check before concluding
                # the pages are exhausted, so a slow tail page isn't dropped.
                await self.page.wait_for_timeout(3_000)
                first_id_after = await self.page.evaluate(
                    "() => { const li = document.querySelector('li.ss-search-row');"
                    " return li ? li.getAttribute('data-documentid') : null; }"
                )
                if first_id_after == first_id_before:
                    _logger.info("Next page did not load new rows (pagination ended or slow)")
                    return False
            await self.page.wait_for_timeout(500)
            return True
        except Exception as exc:
            _logger.info("Pagination ended: %s", str(exc)[:80])
            return False

    def _filter_by_type(self, records: list[ScrapedRecord]) -> list[ScrapedRecord]:
        """Keep only records whose doc_type matches the active record type.

        For pre_foreclosure, additionally drop cancelled/cured/trustee-admin
        docs (Discontinuance, Rescission, Substitution of Trustee — they
        substring-match the legit keywords but are the OPPOSITE of active
        distress) and re-orient the party so the BORROWER (person) lands in
        party_name. Orientation MUST run here, before _enrich_parcels_via_assessor
        keys its name → parcel lookup off party_name.
        """
        if not self.active_record_type or self.active_record_type == "all":
            return records
        keywords = _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return records
        is_preforeclosure = self.active_record_type == "pre_foreclosure"
        is_divorce = self.active_record_type == "divorce"
        kept = []
        for r in records:
            doc = (r.doc_type or "").upper()
            if is_divorce:
                # Tyler SelfService is a generic keyword connector with no precise
                # server-side divorce filter — use the shared 3-state classifier
                # and fail closed on ambiguous bare "DISSOLUTION" (keeps corporate/
                # entity dissolutions out), then keep a real person in party_name.
                if not is_divorce_doc(r.doc_type, precise_source=False):
                    continue
                r.party_name, r.heirs = orient_divorce_party(
                    r.party_name, r.heirs, r.doc_type
                )
                kept.append(r)
                continue
            if not any(kw in doc for kw in keywords):
                continue
            if is_preforeclosure:
                # A doc that keyword-matched but signals the foreclosure was
                # cancelled/cured or is pure trustee admin is NOT active distress.
                if is_cancellation_or_admin(r.doc_type):
                    continue
                # Borrower orientation: an NTS is often indexed with the trustee
                # company as grantor and the borrower as grantee. Put the person
                # (homeowner) in party_name; drop bank-vs-trustee records.
                oriented = orient_pre_foreclosure_party(r.party_name, r.heirs)
                if oriented is None:
                    continue
                r.party_name, r.heirs = oriented
            elif self.active_record_type == "probate":
                # Death certs index the issuing agency/filing state as grantor and
                # the DECEDENT as grantee; promote the decedent and strip "Estate
                # of" captions BEFORE the assessor name→parcel lookup. No-op when
                # the grantor is already the decedent.
                r.party_name, r.heirs = orient_probate_party(r.party_name, r.heirs, r.doc_type)
            kept.append(r)
        return kept

    # ─── Assessor-based parcel enrichment ─────────────────────────────────

    # Okanogan uses TaxSifter (publicaccessnow.com). Other Tyler SelfService
    # counties may use different assessor portals — add mappings here.
    _ASSESSOR_URLS: dict[str, str] = {
        "okanogan": "https://okanoganwa-taxsifter.publicaccessnow.com",
    }

    async def _enrich_parcels_via_assessor(self, records: list[ScrapedRecord]) -> None:
        """Look up parcel IDs from the county assessor portal by grantor name.

        Tyler SelfService detail pages are blocked (server session state
        issue), so we use the county assessor's public TaxSifter portal
        as an alternative parcel source. This reuses the same Playwright
        browser context — navigates to TaxSifter, accepts disclaimer,
        searches each grantor name, and extracts the first matching parcel.
        """
        assessor_url = self._ASSESSOR_URLS.get(self.county.lower())
        if not assessor_url:
            _logger.info("No assessor URL configured for %s — skipping parcel enrichment", self.county)
            return

        _logger.info("Enriching %d records via assessor TaxSifter (%s)", len(records), assessor_url)

        from urllib.parse import urlparse

        from src.api.middleware.security import add_scrape_domain
        domain = urlparse(assessor_url).hostname
        if domain:
            add_scrape_domain(domain)

        try:
            # Navigate to TaxSifter and accept disclaimer
            await self.navigate(assessor_url)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            # Accept disclaimer if present
            agree_btn = self.page.locator(
                "input[value*='Agree' i], a:has-text('Agree'), button:has-text('Agree')"
            ).first
            if await agree_btn.count() > 0:
                try:
                    async with self.page.expect_navigation(timeout=10_000):
                        await agree_btn.click()
                except Exception:
                    await self.page.wait_for_timeout(2_000)

            found = 0
            for record in records:
                if record.parcel_id:
                    continue
                name = record.party_name
                if not name:
                    continue
                # Use the first grantor name (before any comma-separated list)
                # e.g. "SALSKY, RICHARD B" → search "SALSKY"
                search_name = name.split(",")[0].strip()
                if not search_name or len(search_name) < 3:
                    continue

                parcel = await self._taxsifter_search(search_name)
                if parcel:
                    record.parcel_id = parcel
                    found += 1

            _logger.info("Assessor enrichment: %d/%d parcels found", found, len(records))

        except Exception as exc:
            _logger.warning("Assessor enrichment error: %s", str(exc)[:120])

    async def _taxsifter_search(self, name: str) -> str | None:
        """Search TaxSifter for an owner name and return the first parcel ID.

        TaxSifter is ASP.NET and ignores URL query params — must use the
        actual search form (fill input + press Enter) for each lookup.
        """
        try:
            search_input = self.page.locator("input[type='text']").first
            if await search_input.count() == 0:
                return None
            await search_input.fill(name)
            await search_input.press("Enter")
            await self.page.wait_for_timeout(3_000)

            # Extract first parcel number (10+ digits)
            parcel = await self.page.evaluate(r"""() => {
                const text = document.body.innerText;
                const match = text.match(/\b(\d{10,})\b/);
                return match ? match[1] : null;
            }""")

            return parcel
        except Exception:
            return None
