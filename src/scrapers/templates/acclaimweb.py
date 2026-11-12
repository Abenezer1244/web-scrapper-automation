"""AcclaimWeb template scraper for Tyler Technologies AcclaimWeb recorder portals.

Covers WA counties using the AcclaimWeb/Harris Recording Solutions interface.
Standardized Kendo UI navigation + extraction.

AcclaimWeb sites share:
- Disclaimer page with "Accept" link (public access, no login)
- Record Date search at /search/SearchTypeRecordDate
- Kendo DatePicker widgets (#FromDatePicker, #ToDatePicker)
- Kendo Grid results (#SearchResultGrid)
- Kendo pager for pagination

Counties using AcclaimWeb in WA:
Chelan, Douglas, Pend Oreille
"""

import asyncio
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import (
    BridgeScraper,
    ScrapedRecord,
    normalize_party_text,
)
from src.scrapers.divorce import is_divorce_doc, orient_divorce_party
from src.scrapers.preforeclosure import (
    is_cancellation_or_admin,
    orient_pre_foreclosure_party,
)
from src.scrapers.probate import orient_probate_party
from src.scrapers.reliability import (
    ScraperBlockedError,
    ScraperExecutionError,
    TransientScrapeError,
    classify_results_page,
    detect_block,
)
from src.utils.logger import setup_logger

# Substrings an AcclaimWeb results page renders for a genuine zero-result window.
_EMPTY_MARKERS = ("no results", "0 records", "no records", "no documents found")

# Every AcclaimWeb search loads its grid shell (PartialGrid) and then, when there are
# rows, the rows as JSON {"data": [...], "total": N} from GridResults (Chelan Telerik
# and Douglas alike, verified live 2026-11-12). A search with no rows loads the shell
# and no GridResults. Reading those responses, not the DOM, is what ties a result to
# the search we just ran: the DOM still shows the previous search's rows for a moment.
_GRID_SHELL_PATH = "/search/partialgrid"
_GRID_DATA_PATH = "/search/gridresults"
_MAX_GRID_PAGES = 200
# Deadline for any one portal answer: a response body, or a later grid page.
_PORTAL_TIMEOUT_S = 30
# GridResults RecordDate is "/Date(<epoch ms>)/" for the recording time in the county.
_GRID_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")
_COUNTY_TZ = ZoneInfo("America/Los_Angeles")

_logger = setup_logger("scraper.template.acclaimweb")

# Company-name tokens for the person-vs-company grantor/grantee heuristic. Module
# level (built once) so the nested _is_person helper references a constant instead
# of capturing a loop variable (ruff B023).
_COMPANY_WORDS = {"INC", "LLC", "CORP", "CORPORATION", "BANK",
    "TRUST", "INSURANCE", "MORTGAGE", "SYSTEMS", "FINANCIAL",
    "NATIONAL", "SERVICES", "CLEARING", "REPUBLIC", "FARGO",
    "TITLE", "FEDERAL", "ASSOCIATION", "CREDIT", "UNION",
    "COMPANY", "CO", "CAPITAL", "PARTNERS", "GROUP",
    "SOLUTIONS", "AGENCY", "AUTHORITY", "DEPARTMENT"}

# Per-site doc type keywords. DO NOT merge with the equivalent maps in
# other templates (eagleweb, landmarkweb, tyler_selfservice, etc.) —
# each template's keyword list is tuned to its underlying site's doc-type
# vocabulary. Examples of intentional divergence: AcclaimWeb (Chelan)
# uses abbreviated codes like "TOD"/"AFFD"/"PTREC"/"NTS"/"NOD"/"FTL"
# that no other site emits; AcclaimWeb deliberately excludes bare
# "ESTATE" (it matches "REAL ESTATE CONTRACT") and "SUBSTITUTION OF
# TRUSTEE" (low-quality leads), while ava_fidlar/landmarkweb include
# both. A shared map would produce false positives or miss records.
_DOC_TYPE_MAP = {
    # Bare "ESTATE" is excluded — it matched "REAL ESTATE CONTRACT" via
    # whole-word match. "ESTATE OF" (phrase) only appears on actual
    # probate filings like "ESTATE OF JOHN DOE".
    "probate": ["PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
                "PERSONAL REPRESENTATIVE", "PERSONAL REP",
                "ESTATE OF", "WILL", "DEATH",
                "AFFIDAVIT OF HEIRSHIP", "HEIR",
                # Chelan abbreviations
                "TOD", "AFFD", "PTREC"],
    # SUBSTITUTION OF TRUSTEE and APPT (appointment) are intentionally
    # excluded — they signal a pre-foreclosure stage but don't name the
    # homeowner in grantor/grantee, so they produce low-quality leads.
    "pre_foreclosure": ["LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
                        "TRUSTEE'S SALE",
                        "FORECLOSURE", "NOTICE OF DEFAULT",
                        # Chelan abbreviations for Notice of Trustee Sale
                        # and Notice of Default — both name the borrower
                        # (homeowner) in the grantor column.
                        "NTS", "NOD"],
    "tax_delinquent": ["TAX", "DELINQUENT", "TAX LIEN", "CERTIFICATE OF DELINQUENCY",
                       "CERTIFICATE OF SALE",
                       # Chelan abbreviations
                       "FTL"],
    "divorce": ["DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION"],
}


def _doc_type_matches(doc_type: str, keywords: list[str]) -> bool:
    """Match a doc_type against a keyword list using a safe strategy.

    Multi-word keywords use substring match ("NOTICE OF TRUSTEE" inside
    "NOTICE OF TRUSTEE SALE"). Single-word abbreviations use whole-word
    match — otherwise tokens like "NTS" substring-match COVENANTS or
    RENTS and leak unrelated records into pre_foreclosure results.
    """
    doc_upper = doc_type.upper()
    doc_words = set(doc_upper.split())
    for kw in keywords:
        if " " in kw:
            if kw in doc_upper:
                return True
        else:
            if kw in doc_words:
                return True
    return False


# Chelan's grid shows only its own document codes (its Doc Type list, read live
# 2026-11-12, has 109). Two shared tokens mean something else there:
# "NOD" is NOTICE OF DISCONTINUANCE (a foreclosure called off) and "AFFD" is any
# AFFIDAVIT. Lis pendens shows as "LP", never "LIS PENDENS". Probate signals in the
# recorder are DEATH CERTIFICATE ("DC") and TRANSFER ON DEATH DEED ("TOD", a living
# owner, gated downstream by include_living_owner_tod).
_CHELAN_DOC_TYPE_MAP = {
    **_DOC_TYPE_MAP,
    "probate": ["DC", "TOD"],
    "pre_foreclosure": ["NTS", "NTSCOM", "LP"],
}


def _grid_date(value: object) -> str | None:
    """GridResults RecordDate -> MM/DD/YYYY, or None when it is not a date.

    Chelan sends "/Date(<epoch ms>)/" (the recording time, read in the county's time
    zone); Douglas sends "2026/10/06".
    """
    text = str(value or "")
    try:
        m = _GRID_DATE_RE.search(text)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, _COUNTY_TZ).strftime("%m/%d/%Y")
        m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", text)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%m/%d/%Y")
        m = re.match(r"\d{2}/\d{2}/\d{4}", text)
        return datetime.strptime(m.group(0), "%m/%d/%Y").strftime("%m/%d/%Y") if m else None
    except (ValueError, OverflowError, OSError):
        return None  # not a real date: the caller treats it as schema drift


def _grid_item(row: dict) -> dict:
    """One GridResults row in the shape _records_from_raw reads.

    Chelan names the code DocType ("NTS"); Douglas names the text DocTypeDescription
    ("DEED OF TRUST"). Douglas also sends ParcelNumber, deliberately not read here:
    parcel_id is a dedup/billing key, and Douglas leads were keyed without it.
    """
    return {
        "instrument": str(row.get("InstrumentNumber") or "").strip(),
        "date_recorded": _grid_date(row.get("RecordDate")) or "",
        "doc_type": str(row.get("DocType") or row.get("DocTypeDescription") or ""),
        "grantor": str(row.get("DirectName") or ""),
        "grantee": str(row.get("IndirectName") or ""),
        "legal": str(row.get("DocLegalDescription") or ""),
        "parcel": "",
        "item_id": str(row.get("TransactionItemId") or ""),
    }


# A recording's detail page prints its legal description as "DocLegal: APN 232020860053
# SP 2019-008 LOT 7" (Chelan, live 2026-11-12); one document can name several APNs.
_DOC_LEGAL_RE = re.compile(
    r"DocLegal:(.*?)(?:Grantor:|Grantee:|Borrower:|Trustee|Related|$)", re.S | re.I
)
# Any 9+ digit number (dashes allowed between digits) after the "APN" label. Shorter
# numbers in a legal are plats and lots ("SP 2019-008"). Taking every one, not a parsed
# list, means a separator we did not foresee yields two candidates, never a first guess.
_APN_NUMBER_RE = re.compile(r"\d(?:-?\d){8,}")
_APN_LOOKUP_KEY = "recording_apn_lookup"
_DETAIL_PACE_S = 1.5


def parse_detail_apns(html_text: str) -> list[str] | None:
    """The distinct APN candidates a recording detail page names, in order.

    None when the page is not a detail page (no "Doc Type:" field), so "no APN" is
    never mistaken for a page we could not read. A death certificate's page has no
    legal at all (Doc Type, Agency, Deceased): it names a person, not a parcel -> [].
    """
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")))
    if "Doc Type:" not in text:
        return None
    m = _DOC_LEGAL_RE.search(text)
    label = re.search(r"\bAPN\b", m.group(1), re.I) if m else None
    if not label:
        return []
    apns: list[str] = []
    for number in _APN_NUMBER_RE.findall(m.group(1)[label.end():]):
        apn = number.replace("-", "")
        if apn not in apns:
            apns.append(apn)
    return apns


class AcclaimWebScraper(BridgeScraper):
    """Template scraper for all Tyler AcclaimWeb recorder sites.

    Uses standardized Kendo UI selectors for the
    shared AcclaimWeb interface.
    """

    # The site's document vocabulary; a county whose codes differ subclasses with its own.
    DOC_TYPE_MAP = _DOC_TYPE_MAP
    # Whether each lead's recording detail page is read for its APN (see _read_parcels),
    # and the shape a single APN must have to become the lead's parcel.
    READS_DETAIL_APN = False
    APN_SHAPE = re.compile(r"\d{12}")

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor derived from this site's own DOC_TYPE_MAP."""
        from src.scrapers.doc_scope import from_keyword_map

        return from_keyword_map(cls.DOC_TYPE_MAP, record_type)

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        doc_types: list[str] | None = None,
    ):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        self._single_date_mode = False  # Set by _fill_dates when only 1 date input exists
        # Phase B: narrow the client-side keyword filter to an explicit pre-foreclosure
        # selection (subset of _DOC_TYPE_MAP — registry tokens are an exact partition).
        # is not None gate; None = legacy/full; unmappable/empty raises (fail-closed).
        self._doc_type_keyword_override: list[str] | None = None
        if doc_types is not None and self.active_record_type == "pre_foreclosure":
            from src.scrapers.doc_types import canonical_tokens_or_raise
            self._doc_type_keyword_override = canonical_tokens_or_raise(
                county, state, list(dict.fromkeys(doc_types))
            )

        from urllib.parse import urlparse
        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from an AcclaimWeb site using date chunking.

        AcclaimWeb can return large result sets — split into 7-day chunks
        to keep things manageable and avoid timeouts.
        """
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 7

        _logger.info(
            "AcclaimWeb scraper — %s/%s — %s to %s (%d-day chunks)",
            self.county, self.state, date_from, date_to, chunk_days,
        )

        # Navigate to Record Date search page
        search_url = f"{self.base_url}/search/SearchTypeRecordDate"
        await self.navigate(search_url)

        # Accept disclaimer if shown
        await self._accept_disclaimer()

        # Ensure we're on the search page
        if "SearchTypeRecordDate" not in self.page.url:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15_000)
            await self.page.wait_for_timeout(2_000)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start

        # Pre-detect single-date mode BEFORE the loop so day-by-day
        # chunking applies from chunk #1. Chelan's AcclaimWeb has a
        # #RecordDate field (no Kendo DatePicker, no date-range pair).
        try:
            has_single = await self.page.locator("#RecordDate").count() > 0
            has_pair = (
                await self.page.locator("#FromDatePicker, #RecordDateFrom, #txtStartDate, #StartDate").count() > 0
            )
            if has_single and not has_pair:
                self._single_date_mode = True
                _logger.info("Pre-detected single-date mode (#RecordDate without date-range pair)")
        except Exception:
            pass

        # Fail-loud contract (intentional): a setup failure / block / ambiguous
        # results page on ANY day-chunk RAISES and fails the whole job (the
        # scheduler re-runs it) — we never swallow a bad chunk and continue, which
        # would silently under-deliver a partial date window under a DONE job.
        # A single-date site searches one day per chunk, so date_to itself is a chunk.
        while chunk_start < end or (self._single_date_mode and chunk_start == end):
            # Use 1-day chunks in single-date mode, 7-day chunks otherwise
            effective_days = 1 if self._single_date_mode else chunk_days
            chunk_end = min(chunk_start + timedelta(days=effective_days), end)
            cf = chunk_start.strftime("%m/%d/%Y")
            # The single date field is searched for cf alone.
            ct = cf if self._single_date_mode else chunk_end.strftime("%m/%d/%Y")

            _logger.info("Chunk: %s to %s", cf, ct)

            # Navigate back to search for subsequent chunks
            if chunk_start != start:
                await self._go_to_search()

            await self._fill_dates(cf, ct)
            chunk_records = await self._search_and_read(cf, ct)

            new_count = self.dedupe_extend(chunk_records, seen_hashes, all_records)

            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new_count, len(all_records))

            # One day at a time in single-date mode (chunk_end is clamped to end there).
            chunk_start = chunk_start + timedelta(days=1) if self._single_date_mode else chunk_end

        if self.READS_DETAIL_APN:
            await self._read_parcels(all_records)

        # Enrich records missing addresses via county assessor (PACS) lookup.
        # Only look up person names — companies won't match in the assessor.
        _CO_WORDS = {"INC", "LLC", "CORP", "CORPORATION", "BANK", "TRUST",
            "INSURANCE", "MORTGAGE", "SYSTEMS", "FINANCIAL", "NATIONAL",
            "SERVICES", "CLEARING", "REPUBLIC", "FARGO", "TITLE", "FEDERAL",
            "ASSOCIATION", "CREDIT", "UNION", "SECRETARY", "HOUSING", "URBAN",
            "COMMISSION", "DEVELOPMENT", "AUTHORITY", "AGENCY", "LP"}
        needs_address = [
            r for r in all_records
            if not r.property_address and r.party_name and len(r.party_name) >= 3
            and not any(w in _CO_WORDS for w in r.party_name.upper().split())
        ]
        if needs_address:
            _logger.info("Owner lookup: %d person-named records (skipping %d companies)",
                         len(needs_address), len(all_records) - len(needs_address))
            await self._lookup_addresses_by_owner(needs_address)

        _logger.info("acclaimweb complete - %d records (enrichment runs after save)", len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Click disclaimer accept link/button/checkbox if present.

        AcclaimWeb sites vary:
        - Some have an input[type='submit'] button (e.g. Pend Oreille)
        - Some have a checkbox labeled 'Accept Disclaimer' (e.g. Chelan)
        - Some have a link with 'accept'/'agree' text
        """
        _logger.info("Page URL: %s", self.page.url)
        _logger.info("Page title: %s", await self.page.title())

        try:
            body_text = await self.page.inner_text("body")
            _logger.debug("Page text: %d chars", len(body_text))  # P0-04: never the text

            # Strategy 1: Checkbox-based disclaimer (Chelan pattern)
            # Some AcclaimWeb sites use a checkbox labeled "Accept Disclaimer"
            accept_checkbox = self.page.locator(
                "input[type='checkbox']"
            )
            checkbox_count = await accept_checkbox.count()
            if checkbox_count > 0:
                for i in range(checkbox_count):
                    cb = accept_checkbox.nth(i)
                    # Check label text or nearby text
                    cb_id = await cb.get_attribute("id") or ""
                    cb_name = await cb.get_attribute("name") or ""
                    parent_text = await cb.evaluate(
                        "el => (el.parentElement?.textContent || el.closest('label')?.textContent || '').trim()"
                    )
                    _logger.info("Checkbox %d: id=%s name=%s label %d chars", i, cb_id, cb_name,
                                 len(parent_text))  # P0-04: portal text stays out of logs

                    if any(kw in (cb_id + cb_name + parent_text).lower()
                           for kw in ["accept", "disclaim", "agree", "acknowledge"]):
                        if not await cb.is_checked():
                            await cb.check()
                            _logger.info("Checked disclaimer checkbox: %s", cb_id or cb_name)

                        # After checking, look for a submit/continue button
                        await self.page.wait_for_timeout(1_000)
                        submit_after = self.page.locator(
                            "input[type='submit'], button[type='submit'], "
                            "button:has-text('Continue'), button:has-text('Search'), "
                            "a:has-text('Continue'), a:has-text('Search')"
                        )
                        if await submit_after.count() > 0:
                            _logger.info("Clicking submit after checkbox")
                            try:
                                async with self.page.expect_navigation(timeout=15_000):
                                    await submit_after.first.click()
                                _logger.info("Disclaimer accepted via checkbox + submit")
                            except Exception:
                                await self.page.wait_for_timeout(3_000)
                                _logger.info("Disclaimer checkbox + submit (no nav event)")
                        else:
                            # Checkbox alone may trigger JS navigation
                            await self.page.wait_for_timeout(3_000)
                            _logger.info("Disclaimer checkbox checked (no submit button found)")

                        _logger.info("After disclaimer URL: %s", self.page.url)
                        return

            # Strategy 2: Submit button (standard pattern)
            accept_btn = self.page.locator(
                "input[type='submit'][value*='accept' i], "
                "input[type='submit'][value*='agree' i], "
                "input[type='submit'][value*='acknowledge' i], "
                "button:has-text('accept'), button:has-text('agree'), "
                "a:has-text('accept'), a:has-text('agree'), "
                "a:has-text('continue'), a:has-text('public')"
            )
            if await accept_btn.count() > 0:
                btn_val = await accept_btn.first.get_attribute("value") or await accept_btn.first.inner_text()
                _logger.info("Found disclaimer button: %s", btn_val[:60])

                try:
                    async with self.page.expect_navigation(timeout=15_000):
                        await accept_btn.first.click()
                    _logger.info("Disclaimer accepted via navigation")
                except Exception:
                    await self.page.wait_for_timeout(3_000)
                    _logger.info("Disclaimer clicked (no navigation event)")

                _logger.info("After disclaimer URL: %s", self.page.url)
            else:
                _logger.info("No disclaimer button or checkbox found on page")
        except Exception as exc:
            _logger.info("Disclaimer error: %s", str(exc)[:100])

    async def _go_to_search(self) -> None:
        """Navigate back to the Record Date search form."""
        search_url = f"{self.base_url}/search/SearchTypeRecordDate"
        try:
            # Try clicking search tab link first (stays in session).
            # On Chelan the link is often hidden behind the results panel
            # so check is_visible() with a short timeout to avoid a 30s hang.
            search_link = self.page.locator(
                "a[href*='SearchTypeRecordDate'], a:has-text('Record Date')"
            ).first
            if await search_link.count() > 0 and await search_link.is_visible():
                await search_link.click(timeout=5_000)
                await self.page.wait_for_timeout(2_000)
                return
        except Exception:
            pass

        # Fallback: navigate directly (reliable even when the link is hidden). Douglas
        # sometimes takes >15 s to serve this page (seen live 2026-11-12, and 1 of its 3
        # October jobs failed): one retry, then the timeout propagates and fails the job.
        for attempt in (1, 2):
            try:
                await self.page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                break
            except Exception as exc:
                if attempt == 2 or "Timeout" not in type(exc).__name__:
                    raise
                _logger.warning("Search form timed out; retrying once")
        await self.page.wait_for_timeout(2_000)

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the Kendo DatePicker date inputs."""
        _logger.info("Fill dates page URL: %s", self.page.url)
        try:
            # Short settle after navigation (trimmed from 3s — the subsequent
            # element probe + fill already tolerate a not-fully-idle page).
            await self.page.wait_for_timeout(1_000)

            # Log what elements exist on page
            page_info = await self.page.evaluate("""
                (() => {
                    const info = {
                        has_jquery: typeof $ !== 'undefined',
                        inputs: [],
                        selects: [],
                        buttons: [],
                    };
                    document.querySelectorAll('input').forEach(el => {
                        info.inputs.push({id: el.id, name: el.name, type: el.type, value: el.value});
                    });
                    document.querySelectorAll('select').forEach(el => {
                        info.selects.push({id: el.id, name: el.name});
                    });
                    document.querySelectorAll('button, input[type="submit"]').forEach(el => {
                        info.buttons.push({id: el.id, text: (el.textContent || el.value || '').trim()});
                    });
                    return info;
                })()
            """)
            _logger.info("Page elements: jQuery=%s inputs=%d selects=%d buttons=%d",
                         page_info.get('has_jquery'), len(page_info.get('inputs', [])),
                         len(page_info.get('selects', [])), len(page_info.get('buttons', [])))
            for inp in page_info.get('inputs', [])[:10]:
                _logger.info("  input: id=%s name=%s type=%s val=%s", inp.get('id'), inp.get('name'), inp.get('type'), inp.get('value', '')[:30])
            for btn in page_info.get('buttons', []):
                _logger.info("  button: id=%s text=%s", btn.get('id'), btn.get('text', '')[:30])

            # Try Kendo DatePicker API first
            filled = await self.page.evaluate(f"""
                (() => {{
                    if (typeof $ === 'undefined') return 'no_jquery';

                    // Try setting date range dropdown first
                    const dd = $('#DateRangeDropDown').data('kendoDropDownList');
                    if (dd) {{
                        dd.value('SpecificDateRange');
                        dd.trigger('change');
                    }}

                    const fromPicker = $('#FromDatePicker').data('kendoDatePicker');
                    const toPicker = $('#ToDatePicker').data('kendoDatePicker');
                    if (fromPicker && toPicker) {{
                        fromPicker.value('{date_from}');
                        fromPicker.trigger('change');
                        toPicker.value('{date_to}');
                        toPicker.trigger('change');
                        return 'kendo_ok';
                    }}

                    const singlePicker = $('#RecordDatePicker').data('kendoDatePicker');
                    if (singlePicker) {{
                        singlePicker.value('{date_from}');
                        singlePicker.trigger('change');
                        return 'kendo_single';
                    }}

                    return 'no_kendo_pickers';
                }})()
            """)
            _logger.info("Kendo fill result: %s", filled)

            if filled in ('kendo_ok', 'kendo_single'):
                await self.page.wait_for_timeout(1_000)
                return

            # Fallback: type into paired input elements directly
            for from_id, to_id in [
                ("FromDatePicker", "ToDatePicker"),
                ("RecordDateFrom", "RecordDateTo"),
                ("txtStartDate", "txtEndDate"),
                ("StartDate", "EndDate"),
            ]:
                from_el = self.page.locator(f"#{from_id}")
                to_el = self.page.locator(f"#{to_id}")
                if await from_el.count() > 0 and await to_el.count() > 0:
                    await from_el.click()
                    await from_el.fill("")
                    await from_el.press_sequentially(date_from, delay=30)
                    await to_el.click()
                    await to_el.fill("")
                    await to_el.press_sequentially(date_to, delay=30)
                    _logger.info("Dates typed via fallback: %s to %s (ids: %s, %s)", date_from, date_to, from_id, to_id)
                    return

            # Single date field fallback (e.g. Chelan/Douglas AcclaimWeb has only #RecordDate)
            # Type the start date — the search returns records ON that date.
            # The chunking loop will iterate day-by-day for single-date sites.
            #
            # Chelan's #RecordDate is wrapped in a DatePicker widget that
            # silently swallows press_sequentially keystrokes — the old value
            # stays in the input even after typing. We verify the value
            # actually changed and fall back to JS-based value injection +
            # change event dispatch if it didn't.
            single_el = self.page.locator("#RecordDate")
            if await single_el.count() > 0:
                await single_el.click()
                await single_el.fill("")
                await single_el.press_sequentially(date_from, delay=30)
                # Verify the value actually stuck
                actual = (await single_el.get_attribute("value") or "").strip()
                if actual != date_from:
                    _logger.info(
                        "RecordDate press_sequentially failed (value=%r, expected=%r) — forcing via JS",
                        actual, date_from,
                    )
                    await single_el.evaluate(
                        f"el => {{ el.value = '{date_from}'; "
                        f"el.dispatchEvent(new Event('change', {{bubbles: true}})); "
                        f"el.dispatchEvent(new Event('input', {{bubbles: true}})); }}"
                    )
                _logger.info("Single date set: %s (RecordDate field)", date_from)
                self._single_date_mode = True
                return

            # Last resort: find any visible date-looking text inputs
            date_inputs = await self.page.locator(
                "input[type='text']:visible, input[type='date']:visible"
            ).all()
            if date_inputs:
                _logger.info("Found %d visible date inputs for last-resort fill", len(date_inputs))
                await date_inputs[0].click()
                await date_inputs[0].fill("")
                await date_inputs[0].press_sequentially(date_from, delay=30)
                if len(date_inputs) >= 2:
                    await date_inputs[1].click()
                    await date_inputs[1].fill("")
                    await date_inputs[1].press_sequentially(date_to, delay=30)
                    _logger.info("Dates typed via last-resort: %s to %s", date_from, date_to)
                else:
                    _logger.info("Single date typed via last-resort: %s", date_from)
                    self._single_date_mode = True
                return

            raise ScraperExecutionError(
                self.county, "AcclaimWeb",
                "could not locate date inputs (search never ran)",
                record_type=self.active_record_type, date_from=date_from, date_to=date_to,
            )

        except ScraperExecutionError:
            raise
        except Exception as exc:
            raise ScraperExecutionError(
                self.county, "AcclaimWeb", "date-fill failed",
                record_type=self.active_record_type, date_from=date_from, date_to=date_to,
                context=str(exc)[:120],
            ) from exc

    async def _submit_search(self) -> None:
        """Click the Search button. _search_and_read waits on the responses it causes."""
        search_btn = self.page.locator("#SearchBtn")
        if await search_btn.count() == 0:
            # :visible: Chelan also has a hidden HelpButton whose text contains "Search".
            search_btn = self.page.locator(
                "button:has-text('Search'):visible, input[type='submit'][value='Search']:visible"
            )
        if await search_btn.count() == 0:
            body = await self.page.inner_text("body")
            raise ScraperExecutionError(
                self.county, "AcclaimWeb",
                "no search button found (search never ran)",
                record_type=self.active_record_type, context=body[:120],
            )
        await search_btn.first.click()

    async def _search_and_read(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run the filled-in search and return every row it found, of every page.

        Only requests issued after our click count: a late answer to an earlier
        request is never read as this search's. The grid shell (PartialGrid) of a
        search with rows binds the grid to GridResults; an empty search's shell has no
        such binding (Chelan and Douglas alike, live 2026-11-12). So zero is returned
        only for a successful shell with no binding AND the page's empty marker.
        Anything else RAISES: network/timeout/HTTP failures as TransientScrapeError
        (the worker re-runs the job), short, stale or malformed results as
        ScraperExecutionError.
        """
        origin = urlparse(self.base_url).hostname
        ours: set = set()
        loop = asyncio.get_running_loop()
        grid_data: asyncio.Future = loop.create_future()

        def _ours(resp, method: str, path: str) -> bool:
            req = resp.request
            u = urlparse(resp.url)
            return (req in ours and req.method == method and u.hostname == origin
                    and u.path.lower().endswith(path))

        def _on_request(req) -> None:
            ours.add(req)

        def _on_response(resp) -> None:
            if not grid_data.done() and _ours(resp, "POST", _GRID_DATA_PATH):
                grid_data.set_result(resp)

        def _transient(why: str, exc: BaseException | None = None) -> TransientScrapeError:
            return TransientScrapeError(
                self.county, "AcclaimWeb", why, record_type=self.active_record_type,
                date_from=date_from, date_to=date_to,
                context=str(exc)[:120] if exc else None,
            )

        self.page.on("request", _on_request)
        self.page.on("response", _on_response)
        try:
            try:
                async with self.page.expect_response(
                    lambda r: _ours(r, "GET", _GRID_SHELL_PATH), timeout=30_000,
                ) as shell_info:
                    await self._submit_search()
                shell = await shell_info.value
                shell_body = await asyncio.wait_for(shell.text(), _PORTAL_TIMEOUT_S)
            except ScraperExecutionError:
                raise
            except Exception as exc:
                raise _transient("the search never loaded its results grid", exc) from exc
            if shell.status != 200:
                raise _transient(f"results grid shell answered HTTP {shell.status}")
            has_rows = _GRID_DATA_PATH.rsplit("/", 1)[-1] in shell_body.lower()
            first = None
            if has_rows:
                try:
                    first = await asyncio.wait_for(grid_data, 30)
                except TimeoutError as exc:
                    raise _transient("the grid has rows but they never loaded", exc) from exc
        finally:
            self.page.remove_listener("request", _on_request)
            self.page.remove_listener("response", _on_response)

        if first is None:
            page_text = await self.page.inner_text("body")
            verdict = classify_results_page(
                row_count=0, page_text=page_text, empty_markers=_EMPTY_MARKERS
            )
            if verdict == "block":
                raise ScraperBlockedError(
                    self.county, "AcclaimWeb",
                    f"block wall on results page ({detect_block(page_text)})",
                    record_type=self.active_record_type, context=page_text[:120],
                )
            if verdict != "empty":
                raise ScraperExecutionError(
                    self.county, "AcclaimWeb",
                    "grid loaded with no rows and no empty marker (search may have failed)",
                    record_type=self.active_record_type, date_from=date_from,
                    date_to=date_to, context=page_text[:120],
                )
            _logger.info("No results for %s-%s (verified empty)", date_from, date_to)
            return []

        if first.status != 200:
            raise _transient(f"grid rows answered HTTP {first.status}")
        rows = await self._read_grid_rows(first, date_from, date_to)
        return self._records_from_raw([_grid_item(r) for r in rows])

    async def _read_grid_rows(self, first, date_from: str, date_to: str) -> list[dict]:
        """All rows of the search whose first GridResults response is ``first``.

        Chelan's grid asks for one page (``page=1&size=N`` -> ``{"data", "total"}``);
        the rest are asked the same way with the page number changed. Douglas's asks
        for everything at once (no page field -> ``{"Data", "Total"}``).
        """
        def _fail(why: str) -> ScraperExecutionError:
            return ScraperExecutionError(
                self.county, "AcclaimWeb", why, record_type=self.active_record_type,
                date_from=date_from, date_to=date_to,
            )

        def _total_rows(body) -> tuple[object, object]:
            if not isinstance(body, dict):
                return None, None
            return body.get("total", body.get("Total")), body.get("data", body.get("Data"))

        def _transient(why: str, exc: BaseException | None = None) -> TransientScrapeError:
            return TransientScrapeError(
                self.county, "AcclaimWeb", why, record_type=self.active_record_type,
                date_from=date_from, date_to=date_to, context=str(exc)[:120] if exc else None,
            )

        try:
            first_text = await asyncio.wait_for(first.text(), _PORTAL_TIMEOUT_S)
        except Exception as exc:  # the body never arrived: worth a re-run
            raise _transient("grid rows could not be read", exc) from exc
        try:
            total, rows = _total_rows(json.loads(first_text))
        except ValueError as exc:
            raise _fail("grid rows were not JSON") from exc
        if not isinstance(total, int) or not isinstance(rows, list):
            raise _fail("grid rows had no total/data")
        if total <= 0:
            # The shell bound a grid (it has rows) and the rows say none: not a zero we
            # verified. Zero comes only from a shell with no grid binding.
            raise _fail("the grid was bound but reported no rows")
        form = dict(parse_qsl(first.request.post_data or ""))
        page = 1
        while len(rows) < total:
            if "page" not in form:
                raise _fail(f"read {len(rows)} grid rows of {total} and the grid is not paged")
            page += 1
            if page > _MAX_GRID_PAGES:
                raise _fail(f"more than {_MAX_GRID_PAGES} grid pages")
            form["page"] = str(page)
            try:
                status, text = await asyncio.wait_for(self.page.evaluate(
                    """async ([url, body, ms]) => {
                        const r = await fetch(url, {method: 'POST', body,
                            signal: AbortSignal.timeout(ms), headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'}});
                        return [r.status, await r.text()];
                    }""",
                    [first.url, urlencode(form), _PORTAL_TIMEOUT_S * 1000],
                ), _PORTAL_TIMEOUT_S + 5)
            except Exception as exc:  # the fetch failed or hung (network): worth a re-run
                raise _transient(f"grid page {page} request failed", exc) from exc
            if status != 200:
                raise _transient(f"grid page {page} answered HTTP {status}")
            try:
                more_total, more = _total_rows(json.loads(text))
            except ValueError:
                more_total, more = None, None
            if more_total != total or not isinstance(more, list) or not more:
                raise _fail(f"grid page {page} unreadable or changed")
            rows.extend(more)

        if not all(isinstance(r, dict) for r in rows):
            raise _fail("grid row is not an object (schema drift)")
        ids = {r.get("TransactionItemId") for r in rows}
        if len(rows) != total or len(ids) != total:
            raise _fail(f"read {len(rows)} grid rows ({len(ids)} distinct) of {total}")
        lo = datetime.strptime(date_from, "%m/%d/%Y").date()
        hi = datetime.strptime(date_to, "%m/%d/%Y").date()
        for r in rows:
            day = _grid_date(r.get("RecordDate"))
            if not r.get("InstrumentNumber") or day is None:
                raise _fail("grid row without an instrument number or record date (schema drift)")
            if not lo <= datetime.strptime(day, "%m/%d/%Y").date() <= hi:
                raise _fail("grid row dated outside the searched days (stale results)")
        _logger.info("Read %d of %d grid rows for %s-%s", len(rows), total, date_from, date_to)
        return rows

    def _records_from_raw(self, raw: list[dict]) -> list[ScrapedRecord]:
        """Grid rows -> leads of the active record type (other types are dropped)."""
        records: list[ScrapedRecord] = []

        try:
            for item in raw:
                if not item:
                    continue

                record = ScrapedRecord()

                # Instrument number — a document id, NOT a legal description. Keep
                # it in enrichment_data, and use it as the stable per-record
                # fingerprint: this template sets no raw_html_hash, so without this
                # the within-job idempotency key (tasks.py source_fingerprint) would
                # depend on the legal_description we're now changing.
                inst = item.get("instrument", "").strip()
                record.enrichment_data = {"instrument_number": inst, "source": "acclaimweb"}
                if self.READS_DETAIL_APN and item.get("item_id"):
                    # Only where the APN is read: a new key changes the row hash below.
                    record.enrichment_data["source_document_id"] = item["item_id"]

                # Date recorded — normalize various date formats
                date_str = item.get("date_recorded", "").strip()
                if date_str:
                    # Handle ISO format from Kendo data source
                    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_str)
                    if date_match:
                        record.date_recorded = date_match.group(1)
                    else:
                        # Try ISO format (2025-01-15T00:00:00)
                        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_str)
                        if iso_match:
                            record.date_recorded = f"{iso_match.group(2)}/{iso_match.group(3)}/{iso_match.group(1)}"

                # Doc type — use for filtering
                doc_type = item.get("doc_type", "").strip().upper()

                # Filter by the caller-requested record type. If the
                # caller asked for a filtered type (e.g. pre_foreclosure)
                # and this row has no doc_type, drop it — otherwise
                # Deeds of Trust and other unrelated records leak
                # through the filter and appear as "pre-foreclosures".
                active_rt = self.active_record_type
                if active_rt == "divorce":
                    # Generic keyword connector — fail closed on ambiguous bare
                    # "DISSOLUTION" via the shared 3-state classifier (keeps
                    # corporate/entity dissolutions out of divorce results).
                    if not doc_type or not is_divorce_doc(doc_type, precise_source=False):
                        continue
                elif active_rt:
                    keywords = self._doc_type_keyword_override or self.DOC_TYPE_MAP.get(active_rt, [])
                    if keywords:
                        if not doc_type or not _doc_type_matches(doc_type, keywords):
                            continue

                record.doc_type = doc_type if doc_type else None

                # Normalize party cells: stacked owners separated by structural
                # markup (<br>/<div>) in the source cell stay split as " / "
                # instead of concatenating ("SMITH JOHN" + "ACME BANK" ->
                # "SMITH JOHNACME BANK"). Safe on plain text (Kendo dataSource
                # values pass through unchanged) and on HTML (DOM-fallback cells
                # now carry innerHTML).
                grantor = normalize_party_text(item.get("grantor", ""))
                grantee = normalize_party_text(item.get("grantee", ""))

                # Pre-foreclosure borrower orientation via the shared helper: it
                # keeps the PERSON sub-name from a stacked "BORROWER / TRUSTEE
                # COMPANY" cell (the old whole-cell person check dropped that
                # legit lead because the cell contains "CORP") and drops
                # bank-vs-trustee rows with no homeowner. Other record types keep
                # the default grantor→party_name / grantee→heirs below.
                if active_rt == "pre_foreclosure":
                    if is_cancellation_or_admin(doc_type):
                        continue
                    oriented = orient_pre_foreclosure_party(grantor, grantee)
                    if oriented is None:
                        continue
                    record.party_name, record.heirs = oriented
                elif active_rt == "probate":
                    # Death certs index the issuing agency / filing state as
                    # grantor, with the DECEDENT as grantee; promote the decedent
                    # and strip "Estate of" captions. No-op when grantor is the
                    # decedent.
                    record.party_name, record.heirs = orient_probate_party(
                        grantor, grantee, doc_type
                    )
                    if not record.party_name:
                        # Guard #2: no decedent on either side. The append gate
                        # below keeps any row with a DATE, so a party-less
                        # probate lead would ship and still be billed (Codex).
                        continue
                elif active_rt == "divorce":
                    # Both spouses are valid leads; only correct the case where the
                    # recorder indexed a court/state/agency as grantor. No-op when
                    # the grantor is already a person.
                    record.party_name, record.heirs = orient_divorce_party(
                        grantor, grantee, doc_type
                    )
                else:
                    if grantor:
                        record.party_name = grantor
                    if grantee:
                        record.heirs = grantee

                # Legal description — real legal only (None when the index has none;
                # never the instrument # as a stand-in).
                legal = item.get("legal", "").strip()
                if legal:
                    record.legal_description = legal

                # Parcel ID
                parcel = item.get("parcel", "").strip()
                if parcel:
                    record.parcel_id = parcel

                # Stable, unique per-row fingerprint (includes instrument_number via
                # enrichment_data in to_dict); never the bare instrument, which would
                # collapse multi-party/parcel rows under one recording. Keeps tasks.py
                # source_fingerprint stable + distinct without legal_description.
                record.raw_html_hash = self.make_hash(record.to_dict())

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("%d %s records from %d grid rows", len(records), self.active_record_type, len(raw))

        except ScraperExecutionError:
            raise  # block / ambiguous — propagate, never silent []
        except Exception as exc:
            raise ScraperExecutionError(
                self.county, "AcclaimWeb", "grid row processing failed",
                record_type=self.active_record_type, context=str(exc)[:120],
            ) from exc

        return records

    async def _read_parcels(self, records: list[ScrapedRecord]) -> None:
        """Set each lead's parcel from its own recording (the detail page's "APN").

        The parcel then drives the existing parcel-keyed enrichment (statewide GIS
        situs, PACS mailing): evidence from the recorded document, not a guess from an
        owner's name. Outcome per row (enrichment_data["recording_apn_lookup"]):

        - found: exactly one APN of the site's shape -> parcel_id.
        - ambiguous: several candidates, or one of another shape -> no parcel; the
          candidates are kept for review. Which parcel the lead is about is not ours
          to pick.
        - none: the recording names no parcel (a death certificate) -> the owner-name
          fallback in enrichment may look the person up.

        A page that cannot be read, after one retry, RAISES TransientScrapeError (a
        403/429 raises ScraperBlockedError) before anything is saved: the worker re-runs
        the job. A row is never saved half-read, because a re-run of the same job could
        not repair it (same fingerprint) and a later run would give the same recording
        a second identity (Codex 2b). One request per recording, paced.
        """
        by_doc: dict[str, list[ScrapedRecord]] = {}
        for r in records:
            doc_id = (r.enrichment_data or {}).get("source_document_id")
            if doc_id and not r.parcel_id:
                by_doc.setdefault(doc_id, []).append(r)

        for i, (doc_id, recs) in enumerate(by_doc.items()):
            if i:
                await asyncio.sleep(_DETAIL_PACE_S)
            url = f"{self.base_url}/details/documentdetails/{doc_id}/1/1/11"
            apns: list[str] | None = None
            why = ""
            for attempt in (1, 2):
                if attempt == 2:
                    await asyncio.sleep(_DETAIL_PACE_S * 2)
                try:
                    # From the page, not page.request: an in-page fetch passes the
                    # context's SSRF route guard and carries the portal session.
                    status, page_html = await asyncio.wait_for(self.page.evaluate(
                        """async ([url, ms]) => {
                            const r = await fetch(url, {signal: AbortSignal.timeout(ms)});
                            return [r.status, await r.text()];
                        }""",
                        [url, _PORTAL_TIMEOUT_S * 1000],
                    ), _PORTAL_TIMEOUT_S + 5)
                except Exception as exc:  # noqa: BLE001 -- retried, then raised below
                    status, page_html, why = 0, "", type(exc).__name__
                if status in (403, 429):
                    raise ScraperBlockedError(
                        self.county, "AcclaimWeb", f"recording detail page refused (HTTP {status})",
                        record_type=self.active_record_type,
                    )
                apns = parse_detail_apns(page_html) if status == 200 else None
                if apns is not None:
                    break
                why = "not a detail page" if status == 200 else (why or f"HTTP {status}")
            if apns is None:
                raise TransientScrapeError(
                    self.county, "AcclaimWeb", f"recording detail page unreadable ({why})",
                    record_type=self.active_record_type,
                )
            if not apns:
                outcome = "none"
            elif len(apns) == 1 and self.APN_SHAPE.fullmatch(apns[0]):
                outcome = "found"
            else:
                outcome = "ambiguous"
            for rec in recs:
                rec.enrichment_data[_APN_LOOKUP_KEY] = outcome
                if outcome == "found":
                    rec.parcel_id = apns[0]
                elif outcome == "ambiguous":
                    rec.enrichment_data["apn_candidates"] = apns
        if by_doc:
            outcomes = [r.enrichment_data[_APN_LOOKUP_KEY] for rs in by_doc.values() for r in rs]
            _logger.info("Recording APN: %s", {o: outcomes.count(o) for o in sorted(set(outcomes))})

    async def _lookup_addresses_by_owner(self, records: list[ScrapedRecord]) -> None:
        """In-scrape owner-name address lookup, for a TerraScan TaxSifter county (Douglas).

        Chelan has none here: its leads carry the recording's own APN (_read_parcels),
        and its PACS owner-name fallback runs after save in enrichment, paced and behind
        the restricted-source switch (KNOWN_ASSESSOR_URLS). The lookup that used to run
        here posted without the portal's search mode, so it never found anyone.
        """
        from src.scrapers.enrichment.taxsifter import TAXSIFTER_ORIGINS

        if self.county.lower() not in TAXSIFTER_ORIGINS:
            _logger.info("No owner-name address source for %s in the scrape", self.county)
            return
        await self._lookup_taxsifter_addresses(records)

    async def _lookup_taxsifter_addresses(self, records: list[ScrapedRecord]) -> None:
        """The PACS lookup's counterpart for a TerraScan TaxSifter county (Douglas).

        Same contract: address, mailing and assessed value only, and only for a
        unique owner match; parcel_id is never set from an owner-name lookup.
        Lookups run serialized on one worker thread (one session, one disclaimer
        acceptance, polite spacing), off the event loop.
        """
        from src.scrapers.enrichment.taxsifter import fill_addresses_by_owner

        await fill_addresses_by_owner(self.county, records)


class ChelanAcclaimWebScraper(AcclaimWebScraper):
    """Chelan County's AcclaimWeb: same portal, its own document codes."""

    DOC_TYPE_MAP = _CHELAN_DOC_TYPE_MAP
    READS_DETAIL_APN = True
    APN_SHAPE = re.compile(r"\d{12}")  # Chelan parcel numbers: 12 digits
