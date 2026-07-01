"""Pierce County, WA — Probate records connector.

Source: ARMS Web (armsweb.co.pierce.wa.us)
Record type: probate

Approach:
  1. Search PROBATE records by date range
  2. Extract records from the results table
  3. Click each row to expand inline detail panel at bottom of page
  4. Read parcel ID from "Legal Descriptions" section in that panel
  5. Batch GIS enrichment for property + mailing addresses
"""

import re

from bs4 import BeautifulSoup, Tag

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.divorce import orient_divorce_party
from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis
from src.scrapers.preforeclosure import orient_pre_foreclosure_party
from src.scrapers.probate import orient_probate_party
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.pierce_wa_probate")

# Register approved domains for SSRF allowlist
add_scrape_domain("armsweb.co.pierce.wa.us")

# ─── Patterns ─────────────────────────────────────────────────────────────────

_ARMS_HOME = "https://armsweb.co.pierce.wa.us/"
_ARMS_SEARCH = "https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx"

_PARCEL_10 = re.compile(r"\b(\d{10})\b")
_DATE_PATTERN = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_LEGAL_KEYWORDS = re.compile(
    r"\b(LT|LOT|BLK|BLOCK|SEC|TWNSHP|RNG|ADDN|PLAT|DIV|SHORT PLAT)\b",
    re.IGNORECASE,
)

# ARMS appends "(+)" to a party when more parties share that role than the results
# row shows (e.g. "QUALITY LOAN SERVICE CORP(+)"). It is a UI marker, never part of
# a real name, so strip it before any person/company classification or display.
_ARMS_PLUS = re.compile(r"\s*\(\+\)\s*")


def _strip_arms_plus(value: str | None) -> str | None:
    """Remove the ARMS "(+)" more-parties marker; return None unchanged."""
    if not value:
        return value
    return _ARMS_PLUS.sub(" ", value).strip() or None


# ARMS party-role markers: "[R]" (reverse/primary party) and "[E]" (associated/
# estate party). They label a name — they are NEVER a name themselves. A cell that
# carries only a marker (e.g. an "[E]" with no associated name indexed) must NOT
# become a lead named "[E]".
_ARMS_ROLE_MARKER = re.compile(r"\[(?:R|E)\]", re.IGNORECASE)


def _clean_arms_name(value: str | None) -> str | None:
    """Return a real party name, or None when the value carries no actual name.

    Strips ONLY the ARMS role markers ("[R]", "[E]") and the "(+)" more-parties
    marker (never arbitrary bracketed text — a real name may legitimately contain
    brackets). A value that reduces to no alphabetic character (a bare marker such
    as "[E]", punctuation, or empty) is treated as no-name and returns None.
    """
    if not value:
        return None
    cleaned = _ARMS_ROLE_MARKER.sub(" ", value)
    cleaned = _ARMS_PLUS.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if not any(c.isalpha() for c in cleaned):
        return None
    return cleaned


class PierceWAARMSScraper(BridgeScraper):
    """Pierce County ARMS Web portal scraper — supports multiple record types.

    Pass record_type to constructor to select which document checkboxes to use.
    """

    # Maps record_type → (checkbox IDs, label)
    RECORD_TYPE_CONFIG: dict[str, dict] = {
        "probate": {
            "ids": ["226"],
            "label": "PROBATE",
        },
        "pre_foreclosure": {
            "ids": ["187", "188", "146", "324"],  # NOD, Notice of Foreclosure, Lis Pendens, Trustee Sale
            "label": "PRE-FORECLOSURE",
        },
        "divorce": {
            "ids": ["87"],  # DECREE OF DISSOLUTION
            "label": "DIVORCE",
        },
    }

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — Pierce selects exact ARMS document-type checkboxes."""
        from src.scrapers.doc_scope import CollectionScope, DocTypeItem

        if record_type not in cls.RECORD_TYPE_CONFIG:
            return None
        _LABELS = {
            "probate": ["Probate"],
            "pre_foreclosure": [
                "Notice of Default", "Notice of Foreclosure",
                "Lis Pendens", "Notice of Trustee Sale",
            ],
            "divorce": ["Decree of Dissolution"],
        }
        labels = _LABELS.get(record_type)
        if labels is None:
            return None
        return CollectionScope(
            kind="document_type",
            items=tuple(DocTypeItem(label=lbl, exact=True) for lbl in labels),
            note="Selected by exact recorder document-type checkboxes.",
        )

    def __init__(self, record_type: str = "probate", doc_types: list[str] | None = None):
        super().__init__()
        self._record_type = record_type
        cfg = self.RECORD_TYPE_CONFIG.get(record_type, self.RECORD_TYPE_CONFIG["probate"])
        self.DOC_TYPE_IDS: list[str] = cfg["ids"]
        self.DOC_TYPE_LABEL: str = cfg["label"]
        # Phase 2b/B: narrow to selected canonical doc types' ARMS checkbox IDs when
        # an explicit selection was made (None = legacy full set). FAIL-CLOSED: an
        # explicit selection that can't be mapped (stale config) RAISES rather than
        # silently broadening to the full set — the user must never get types they
        # didn't pick.
        if doc_types is not None and record_type == "pre_foreclosure":
            from src.scrapers.doc_types import canonical_tokens_or_raise
            self.DOC_TYPE_IDS = canonical_tokens_or_raise("pierce", "wa", doc_types)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        _logger.info("Pierce WA %s — scraping %s to %s", self.DOC_TYPE_LABEL, date_from, date_to)

        await self._accept_disclaimer()
        await self.navigate(_ARMS_SEARCH)
        await self._fill_search_form(date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0

        while True:
            page_num += 1
            _logger.info("Processing page %d", page_num)

            soup = await self.get_soup_async()
            page_records = self._extract_records(soup)

            new_count = 0
            for record in page_records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info("Page %d — %d new records (total: %d)", page_num, new_count, len(all_records))

            # Report progress to worker (updates DB for real-time API polling)
            page_total = getattr(self, "_page_total", 0)
            if self.on_progress:
                self.on_progress(page_num, page_total, len(all_records))

            # Don't click detail pages during pagination — it breaks page state
            # Parcel IDs will be extracted after all pages are collected

            if not await self._go_to_next_page():
                break

            await self.polite_delay()

        # ── Second pass: extract parcel IDs by clicking each row ──────────
        # Navigate back to page 1 and click through all rows
        needs_parcel = [r for r in all_records if not r.parcel_id and r.enrichment_data]
        if needs_parcel:
            _logger.info("Extracting parcel IDs for %d records via detail pages...", len(needs_parcel))
            # Report phase change so frontend shows "Looking up parcel IDs..."
            if self.on_progress:
                self.on_progress(0, len(needs_parcel), len(all_records), "parcel_lookup")
            try:
                # Go to first page
                first_btn = self.page.locator("#OptionsBar1_imgFirst")
                if await first_btn.count() > 0:
                    await first_btn.click(timeout=5_000)
                    await self.page.wait_for_load_state("load")
                    await self.page.wait_for_timeout(1_000)
            except Exception:
                pass

            await self._fetch_parcels_from_detail(needs_parcel)

        parcels_found = sum(1 for r in all_records if r.parcel_id)
        _logger.info("Parcel IDs found: %d/%d", parcels_found, len(all_records))

        # ── Batch GIS enrichment (50 parcels per API call) ────────────────────
        parcel_records = [r for r in all_records if r.parcel_id and len(r.parcel_id) >= 10]
        _logger.info("Batch GIS enriching %d records with parcel IDs", len(parcel_records))
        if self.on_progress:
            self.on_progress(0, len(parcel_records), len(all_records), "enriching")
        if parcel_records:
            parcel_ids = [r.parcel_id for r in parcel_records]
            gis_results = batch_enrich_parcels_gis(parcel_ids, "pierce", "WA")
            enriched_count = 0
            for record in parcel_records:
                gis_data = gis_results.get(record.parcel_id)
                if gis_data and gis_data.get("property_address"):
                    record.property_address = gis_data["property_address"]
                    record.mailing_address = gis_data.get("mailing_address") or record.mailing_address
                    if isinstance(record.enrichment_data, dict):
                        record.enrichment_data.update(gis_data)
                    else:
                        record.enrichment_data = gis_data
                    enriched_count += 1
            _logger.info("Batch GIS: %d/%d records enriched with addresses", enriched_count, len(parcel_records))

        _logger.info("Pierce WA %s — complete. %d records", self.DOC_TYPE_LABEL, len(all_records))
        return all_records

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _accept_disclaimer(self) -> None:
        """Navigate to the ARMS home page and accept the terms disclaimer."""
        page = self.page
        await self.navigate(_ARMS_HOME)

        accept_link = page.locator("a:has-text('Click here to acknowledge')")
        try:
            await accept_link.wait_for(timeout=10_000)
            await accept_link.click()
            await page.wait_for_load_state("load")
            _logger.info("Disclaimer accepted")
        except Exception:
            _logger.info("No disclaimer prompt found — may already be accepted")

    async def _fill_search_form(self, date_from: str, date_to: str) -> None:
        """Fill and submit the ARMS Web search form."""
        page = self.page
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(1_000)

        # Type dates into Infragistics WebDateChooser controls
        # These controls require keyboard input — JS .value= doesn't register.
        # ARMS updated the title attribute in 2026 from "mm/dd/yyyy" to
        # "Date Filed From/To, format mm/dd/yyyy", so match by prefix.
        from_input = page.locator('input[title*="Date Filed From"]').first
        await from_input.wait_for(state="visible", timeout=15_000)
        await from_input.click(force=True)
        await page.wait_for_timeout(200)
        await page.keyboard.press("Control+a")
        await page.keyboard.type(date_from, delay=30)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)

        # Click the To date input explicitly (don't rely on Tab order which
        # can skip to a different field if the page re-renders).
        to_input = page.locator('input[title*="Date Filed To"]').first
        await to_input.click(force=True)
        await page.wait_for_timeout(200)
        await page.keyboard.press("Control+a")
        await page.keyboard.type(date_to, delay=30)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)
        _logger.info("Typed date range: %s — %s", date_from, date_to)

        # Check document type checkboxes (configurable per subclass)
        for doc_id in self.DOC_TYPE_IDS:
            cb = page.locator(f"#cphNoMargin_f_dclDocType_{doc_id}")
            await cb.scroll_into_view_if_needed(timeout=5_000)
            await cb.check(timeout=5_000)
            await page.wait_for_timeout(200)
        _logger.info("Checked %d doc types for %s", len(self.DOC_TYPE_IDS), self.DOC_TYPE_LABEL)

        # Submit and wait for results page
        await page.click("#cphNoMargin_SearchButtons1_btnSearch", timeout=10_000)
        try:
            await page.wait_for_url("**/SearchResults**", timeout=30_000)
        except Exception:
            await page.wait_for_timeout(5_000)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2_000)

        record_count = await page.evaluate(r"""() => {
            const m = document.body.innerText.match(/(\d+) records found/);
            return m ? m[1] : 'unknown';
        }""")
        # Persist for _extract_records: "0" = genuine empty (search ran);
        # "unknown" = the "N records found" marker never rendered, i.e. the page
        # never loaded / was blocked, so a missing results table must FAIL the
        # job rather than be scored as a healthy 0.
        self._record_count = record_count
        _logger.info("Search: %s records found", record_count)

        # Detect total pages from the page dropdown
        page_total = await page.evaluate("""() => {
            const sel = document.getElementById('cphNoMargin_cphNoMargin_OptionsBar1_ItemList');
            return sel ? sel.options.length : 1;
        }""")
        self._page_total = page_total
        _logger.info("Total pages: %d", page_total)

    async def _go_to_next_page(self) -> bool:
        """Click the Next arrow to go to the next results page."""
        page = self.page
        try:
            # ARMS uses input[type=image] with title="Next" for pagination
            next_btn = page.locator("#OptionsBar1_imgNext")
            if await next_btn.count() == 0:
                next_btn = page.locator("input[title='Next']").first

            if await next_btn.count() == 0:
                _logger.info("No next button — last page")
                return False

            # Check if we're on the last page via the page dropdown
            is_last = await page.evaluate("""() => {
                const sel = document.getElementById('cphNoMargin_cphNoMargin_OptionsBar1_ItemList');
                if (!sel) return true;
                return sel.selectedIndex >= sel.options.length - 1;
            }""")
            if is_last:
                _logger.info("Last page reached")
                return False

            await next_btn.click(timeout=5_000)
            await page.wait_for_load_state("load")
            await page.wait_for_timeout(2_000)
            _logger.info("Navigated to next page")
            return True

        except Exception as exc:
            _logger.warning("Next page failed: %s", str(exc)[:60])
            return False

    async def _fetch_parcels_from_detail(self, records: list[ScrapedRecord]) -> None:
        """Extract parcel IDs from the Legal Description tab on each detail page.

        For each results page:
        1. Click first instrument to enter detail view
        2. Iterate ALL instruments via the dropdown selector
        3. Click Legal Description tab → read "Parcel Id:" value
        4. Go back to results, advance to next page, repeat
        """
        page = self.page

        # Build map: instrument number → record
        inst_map: dict[str, ScrapedRecord] = {}
        for rec in records:
            inst = (rec.enrichment_data or {}).get("instrument_number")
            if inst:
                inst_map[inst] = rec

        if not inst_map:
            return

        found = 0
        page_num = 0

        while True:
            page_num += 1

            # Click first instrument link on results page to enter detail view
            first_inst = await page.evaluate(r"""() => {
                const cells = document.querySelectorAll('td.fauxDetailLink');
                for (const c of cells) {
                    if (/^\d{10,12}$/.test(c.textContent.trim())) return c.textContent.trim();
                }
                return null;
            }""")

            if not first_inst:
                break

            try:
                await page.locator(f"td.fauxDetailLink:has-text('{first_inst}')").first.click(timeout=5_000)
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(500)
            except Exception:
                break

            # Get instrument dropdown options
            inst_select = page.locator("#cphNoMargin_OptionsBar1_ItemList")
            options = await inst_select.evaluate(
                """el => Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()}))"""
            )

            for opt in options:
                try:
                    await inst_select.select_option(value=opt["v"], timeout=3_000)
                    await page.wait_for_load_state("load")
                    await page.wait_for_timeout(200)

                    # Click Legal Description tab
                    await page.locator("span:has-text('Legal Description')").first.click(timeout=3_000)
                    await page.wait_for_timeout(200)

                    # Extract "Parcel Id:" from the tab content.
                    # ARMS updated the Legal Description tab in 2026: the
                    # "Parcel Id:" label is now a <th>, not a <td>, and the
                    # value lives in the next sibling element (can be td or
                    # th). Use nextElementSibling and query both tag types.
                    parcel_id = await page.evaluate(r"""() => {
                        const labels = document.querySelectorAll('td, th, span');
                        for (const el of labels) {
                            const t = el.textContent.trim();
                            if (!/^Parcel\s*Id:?$/i.test(t)) continue;
                            // Try next sibling first
                            let val = '';
                            if (el.nextElementSibling) {
                                val = el.nextElementSibling.textContent.trim();
                            }
                            // Fallback: parent's next cell
                            if ((!val || val.length < 6) && el.parentElement && el.parentElement.nextElementSibling) {
                                val = el.parentElement.nextElementSibling.textContent.trim();
                            }
                            if (val && /^\d{6,}$/.test(val)) return val;
                        }
                        return null;
                    }""")

                    if parcel_id and parcel_id.strip():
                        target = inst_map.get(opt["t"])
                        if target and not target.parcel_id:
                            target.parcel_id = parcel_id.strip()
                            found += 1
                            if found <= 3 or found % 10 == 0:
                                _logger.info("  %s → parcel %s", opt["t"], target.parcel_id)
                            if self.on_progress:
                                self.on_progress(found, len(inst_map), found, "parcel_lookup")

                except Exception:
                    pass

            # Back to results
            try:
                await page.locator("a:has-text('Back to Results')").first.click(timeout=5_000)
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(1_000)
            except Exception:
                break

            # Check if last page
            is_last = await page.evaluate("""() => {
                const sel = document.getElementById('cphNoMargin_cphNoMargin_OptionsBar1_ItemList');
                if (!sel) return true;
                return sel.selectedIndex >= sel.options.length - 1;
            }""")
            if is_last:
                break

            # Next results page
            try:
                await page.locator("#OptionsBar1_imgNext").click(timeout=5_000)
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(1_000)
            except Exception:
                break

        _logger.info("  Detail pages: %d parcel IDs found across %d pages", found, page_num)

    def _extract_records(self, soup: BeautifulSoup) -> list[ScrapedRecord]:
        """Extract records from the ARMS results table."""
        tables = soup.find_all("table")
        data_table = None
        for t in tables:
            data_rows = t.find_all("tr")
            if len(data_rows) < 5:
                continue
            if len(data_rows) > 1:
                first_td = data_rows[1].find("td")
                if first_td and first_td.get_text(strip=True).isdigit():
                    data_table = t
                    break

        if data_table is None:
            # Genuine empty day ("0 records found") legitimately has no table →
            # return []. But if the record-count marker never rendered ("unknown"),
            # the search page never loaded / was blocked — fail loud so the job
            # isn't scored as a healthy 0-record success.
            count = getattr(self, "_record_count", "unknown")
            if count == "0":
                _logger.info("No results table — '0 records found' marker present (genuine empty)")
                return []
            raise RuntimeError(
                f"Pierce ARMS: results table missing and record-count marker is "
                f"{count!r} (not '0') — search page never loaded / blocked / errored"
            )

        rows = data_table.find_all("tr")
        records: list[ScrapedRecord] = []

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 9:
                continue
            first_text = cells[0].get_text(strip=True)
            if not first_text.isdigit():
                continue
            record = self._map_row(cells)
            if record:
                records.append(record)

        return records

    def _map_row(self, cells: list[Tag]) -> ScrapedRecord | None:
        """Parse a single table row into a ScrapedRecord."""
        all_texts = []
        for c in cells:
            text = self.clean(c.get_text(separator=" ", strip=True))
            if text:
                all_texts.append(text)

        if not all_texts:
            return None

        record = ScrapedRecord()

        # Instrument number — clickable link (12 digits for modern, 10+ for old)
        inst_re = re.compile(r"\b(\d{10,12})\b")
        for c in cells:
            for link in c.find_all("a"):
                link_text = link.get_text(strip=True)
                m = inst_re.match(link_text)
                if m and len(link_text) >= 10:
                    record.enrichment_data = {"instrument_number": m.group(1)}
                    break
            if record.enrichment_data:
                break
        if not record.enrichment_data:
            for text in all_texts:
                m = re.search(r"\b(20\d{10})\b", text)
                if m:
                    record.enrichment_data = {"instrument_number": m.group(1)}
                    break

        # Date — must be valid MM/DD/YYYY
        for text in all_texts:
            m = _DATE_PATTERN.search(text)
            if m:
                date_str = m.group(1)
                month, day, year = date_str.split("/")
                if 1 <= int(month) <= 12 and 1 <= int(day) <= 31 and 1980 <= int(year) <= 2030:
                    record.date_recorded = date_str
                    break

        # Name — contains [R] and/or [E] markers
        for c in cells:
            cell_text = c.get_text(separator="|", strip=True)
            if "[R]" in cell_text or "[E]" in cell_text:
                record.party_name, record.heirs = self._parse_name_cell(c)
                if self._record_type == "divorce":
                    # ARMS checkbox 87 already constrains the search to DECREE OF
                    # DISSOLUTION (precise), so no doc-type re-filter is needed.
                    # Both spouses are valid leads; only correct the case where a
                    # court/state landed in the [R] (party_name) slot. No-op when
                    # it is already a person.
                    record.party_name, record.heirs = orient_divorce_party(
                        record.party_name, record.heirs, self.DOC_TYPE_LABEL
                    )
                elif self._record_type == "pre_foreclosure":
                    # ARMS indexes a Notice of Trustee Sale with the TRUSTEE /
                    # beneficiary as the [R] party and the borrower as [E] (or the
                    # reverse) — so the raw [R] is frequently a company
                    # ("TRUSTEE CORPS", "QUALITY LOAN SERVICE CORP"), not the
                    # distressed homeowner. Strip the ARMS "(+)" more-parties marker
                    # first (it is never part of a name), then orient so the PERSON
                    # becomes party_name and the company context moves to heirs.
                    # orient returns None only when NEITHER visible role carries a
                    # natural person — observed: bank-vs-bank, trustee-vs-commercial-LLC,
                    # or a parse-junk "[E]" cell. That is the SAME drop-if-no-person
                    # contract King already applies (no homeowner = not a lead). When a
                    # person exists it is always present in [R] or [E] (never only
                    # behind "(+)"), so no real homeowner is lost — verified live.
                    party = _strip_arms_plus(record.party_name)
                    heirs = _strip_arms_plus(record.heirs)
                    oriented = orient_pre_foreclosure_party(party, heirs)
                    if oriented is None:
                        # No person on either visible role — log (never silent) so a
                        # systematic borrower-behind-"(+)" loss is observable.
                        _logger.info(
                            "pre_foreclosure: no person party — [R]=%r [E]=%r (dropped)",
                            party, heirs,
                        )
                        record.party_name = None  # dropped by the party-name guard below
                    else:
                        record.party_name, record.heirs = oriented
                elif self._record_type == "probate":
                    # Probate: ARMS indexes the decedent/estate as [R] (party_name)
                    # and the heir/personal-rep as [E] (heirs). Route through the
                    # shared probate orientation so a Certificate-of-Death filing
                    # agency ("STATE OF WASHINGTON, DEPT OF HEALTH") in the [R] slot
                    # is stripped and a person-like [E] promoted — the SAME rule the
                    # other probate scrapers use. Strip the "(+)" marker first. A
                    # no-op for the common case where [R] is already the decedent;
                    # (None, None) when neither role is a real person -> dropped by
                    # the party-name guard below.
                    party = _strip_arms_plus(record.party_name)
                    heirs = _strip_arms_plus(record.heirs)
                    record.party_name, record.heirs = orient_probate_party(
                        party, heirs, self.DOC_TYPE_LABEL
                    )
                break

        # Legal description
        for text in all_texts:
            if _LEGAL_KEYWORDS.search(text) and text != record.party_name:
                record.legal_description = text
                break

        # Don't extract parcel IDs from inline legal description text —
        # 10-digit numbers there are often Remarks or subdivision codes,
        # not real parcel IDs. Real parcel IDs come from the detail page
        # "Parcel Id:" field (fetched in _fetch_parcels_from_detail).

        # Require valid date + party name
        if not record.date_recorded or not record.party_name:
            return None

        # Skip garbage
        if len(record.party_name) > 200:
            return None
        junk = ["Page 1", "Sort By", "New Search", "Criteria:", "records found",
                "Select All", "#ImageItem", "SelectInstrument", "#Boo"]
        if any(kw in record.party_name for kw in junk):
            return None

        # ARMS results rows carry no reliable per-row doc-type column we can map
        # non-positionally; the search was filtered to this connector's
        # document-type checkboxes, so the configured label IS the doc type.
        record.doc_type = self.DOC_TYPE_LABEL

        return record

    @staticmethod
    def _parse_name_cell(cell: Tag) -> tuple[str | None, str | None]:
        """Parse the Name/Associated Name cell into (party_name, heirs)."""
        text_parts = []
        for child in cell.children:
            if isinstance(child, Tag):
                text_parts.append(child.get_text(strip=True))
            elif isinstance(child, str):
                stripped = child.strip()
                if stripped:
                    text_parts.append(stripped)

        full_text = " ".join(text_parts)
        party_name = None
        heirs = None

        # ``.*?`` (not ``.+?``) so an EMPTY [R] slot ("[R] [E] JONES") captures ""
        # rather than greedily swallowing the following "[E] JONES" — otherwise the
        # associated [E] party would be promoted into party_name (Codex).
        r_match = re.search(r"\[R\]\s*(.*?)(?=\s*\[E\]|$)", full_text)
        if r_match:
            party_name = r_match.group(1).strip()

        e_match = re.search(r"\[E\]\s*(.+?)$", full_text)
        if e_match:
            heirs = e_match.group(1).strip()

        if not party_name and not heirs:
            party_name = full_text.strip() or None

        # Reject bare role markers / no-name residue ("[E]" alone must not become a
        # lead). _clean_arms_name returns None when only markers/punctuation remain.
        return _clean_arms_name(party_name), _clean_arms_name(heirs)


# ─── Record-type-pinned subclasses ───────────────────────────────────────────
# These were previously bare aliases to PierceWAARMSScraper (which defaults to
# record_type="probate"), so PierceWAPreForeclosureScraper() with no args
# actually scraped probate. They are now thin subclasses that pin the correct
# default record_type so the name matches the behavior.
#
# Production is unaffected: the live Pierce connector points at the base class
# PierceWAARMSScraper (migration 010) and the worker passes record_type/doc_types
# explicitly (src/workers/tasks.py). Alembic migrations 001/010 reference these
# names as string literals (getattr / SQL), so the names must keep existing as
# importable module attributes — subclasses preserve that.
#
# Signatures mirror the base constructor (record_type, doc_types) so inspect.
# signature() in _run_scraper still sees both and forwards an explicit doc-type
# selection (Pierce's ARMS checkbox narrowing).
class PierceWAProbateScraper(PierceWAARMSScraper):
    def __init__(self, record_type: str = "probate", doc_types: list[str] | None = None):
        super().__init__(record_type=record_type, doc_types=doc_types)


class PierceWAPreForeclosureScraper(PierceWAARMSScraper):
    def __init__(self, record_type: str = "pre_foreclosure", doc_types: list[str] | None = None):
        super().__init__(record_type=record_type, doc_types=doc_types)


class PierceWADivorceScraper(PierceWAARMSScraper):
    def __init__(self, record_type: str = "divorce", doc_types: list[str] | None = None):
        super().__init__(record_type=record_type, doc_types=doc_types)
