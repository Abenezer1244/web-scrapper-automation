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

# Substrings the Skagit results page renders for a genuine zero-result window.
_EMPTY_MARKERS = ("returned 0 records", "no results", "no records", "no documents")

_logger = setup_logger("scraper.template.skagit")

_DOC_TYPE_MAP = {
    # CLIENT-SIDE refinement map (used ONLY by _filter_by_type). The coarse
    # SERVER dropdown selection is driven by the separate _SERVER_DOC_TYPES below
    # (which still searches the broad "Affidavit" bucket) — so removing bare
    # "AFFIDAVIT" here narrows the post-search filter, NOT what gets searched.
    # Keywords are substring-matched against doc_type + the recorder COMMENT
    # field by _filter_by_type. The COMMENT is where Skagit records the probate
    # nature of an otherwise-generic "Affidavit" (e.g. "INHERITANCE LACK OF
    # PROBATE AFFIDAVIT", "COMMUNITY PROPERTY AGREEMENT AFFIDAVIT"). Bare
    # "AFFIDAVIT"/"ESTATE"/"WILL"/"HEIR" are intentionally EXCLUDED: "AFFIDAVIT"
    # is the whole over-broad doc type (keeps every affidavit), and "WILL"/"HEIR"
    # / bare "ESTATE" substring-match common surnames ("WILLIAMS") and "REAL
    # ESTATE". "PROBATE" already covers "LACK OF PROBATE". The remaining terms
    # are probate-unambiguous as substrings.
    "probate": [
        "PROBATE", "INHERITANCE", "LETTERS TESTAMENTARY",
        "LETTERS OF ADMINISTRATION", "PERSONAL REPRESENTATIVE",
        "DEATH CERTIFICATE", "CERTIFICATE OF DEATH", "TRANSFER ON DEATH",
        "COMMUNITY PROPERTY AGREEMENT", "AFFIDAVIT OF HEIRSHIP",
        "DECEASED", "DECEDENT", "ESTATE OF",
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
        # Coarse list retained so _filter_by_type's "no keywords" guard does not
        # short-circuit and return everything; the AUTHORITATIVE divorce gate is
        # divorce.is_divorce_doc (rejects corporate/entity dissolutions and bare
        # separations — "SEPARATION" was removed here for that reason).
        "DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION", "DECREE-DIVORCE",
    ],
}

# Phase B: partition of the pre_foreclosure client-refine keywords (above) by
# canonical doc type, so an explicit selection narrows the client refine to match
# the narrowed server dropdown. Every canonical type Skagit offers for selection
# (see doc_types._AVAILABILITY[("skagit","wa")]) MUST appear here — a missing key
# raises KeyError at construction (fail loud), keeping the server-label map and this
# refine map in lockstep (Codex: require both mappings or fail closed).
_CANONICAL_REFINE_KEYWORDS: dict[str, list[str]] = {
    "lis_pendens": ["LIS PENDENS"],
    "notice_of_trustee_sale": ["NOTICE OF TRUSTEE", "TRUSTEE SALE", "TRUSTEE'S SALE"],
    "notice_of_default": ["NOTICE OF DEFAULT"],
    "notice_of_foreclosure": ["FORECLOSURE"],
}


# Death-certificate party orientation is delegated to the shared
# src/scrapers/probate.py::orient_probate_party (single source of truth). Skagit's
# inverted issuing-state shape ("WASH. STATE OF" / "CALIFORNIA STATE OF") is
# absorbed by the shared _BARE_STATE_RE, which matches both word orders; the shared
# helper additionally strips partial-concat agencies and "ESTATE OF" captions that
# Skagit's old whole-value-only swap missed.


class SkagitRecordingScraper(BridgeScraper):
    """Template scraper for Skagit County Recording Search.

    Custom ASP.NET portal — zero Claude AI cost, standardized selectors.
    Parcel IDs are available directly in search results.
    """

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — Skagit selects a coarse server dropdown then refines
        results client-side against the document type AND recorder comments. The
        client refinement (_DOC_TYPE_MAP) is what actually gets kept, so describe
        that, with a note about the two-stage match."""
        from src.scrapers.doc_scope import from_keyword_map

        return from_keyword_map(
            _DOC_TYPE_MAP,
            record_type,
            note=(
                "Identified from a recorder document-type dropdown search, then "
                "refined by document type and recorder comments; exact wording "
                "varies."
            ),
        )

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        require_parcel_id: bool = True,
        doc_types: list[str] | None = None,
    ):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        self.require_parcel_id = require_parcel_id

        # Phase B: narrow BOTH Skagit stages on an explicit pre-foreclosure selection.
        # Stage 1 (server dropdown): restrict the per-type searches to the selected
        # canonical types' EXACT dropdown labels. Stage 2 (client refine): restrict
        # _filter_by_type's keyword set to those same types. Narrowing only one stage
        # would contradict the checkbox (Codex). FAIL-CLOSED: an unmappable/empty
        # explicit selection raises; None = legacy/full.
        self._server_label_override: list[str] | None = None
        self._refine_keyword_override: list[str] | None = None
        if doc_types is not None and self.active_record_type == "pre_foreclosure":
            from src.scrapers.doc_types import canonical_tokens_or_raise
            # Dedup the selection (order-preserving) so a repeated canonical type
            # doesn't trigger duplicate dropdown searches / refine keywords (Codex).
            selected = list(dict.fromkeys(doc_types))
            self._server_label_override = canonical_tokens_or_raise(county, state, selected)
            kws: list[str] = []
            for d in selected:
                kws.extend(_CANONICAL_REFINE_KEYWORDS[d])  # strict: KeyError = fail loud
            self._refine_keyword_override = kws

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
        if self._server_label_override is not None:
            # Phase B: explicit user selection — search only the chosen dropdown labels.
            doc_types_to_search = self._server_label_override
        elif self.active_record_type and self.active_record_type in self._SERVER_DOC_TYPES:
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

        # Refine the coarse server results client-side. The doc-type dropdown is
        # broad — "Affidavit" returns EVERY affidavit, not just probate ones.
        # _filter_by_type keeps a record only when its doc_type OR recorder
        # comment carries a probate signal, dropping generic non-probate
        # affidavits (the probate nature lives in the comment, e.g. "LACK OF
        # PROBATE AFFIDAVIT"). Previously this method was defined but never
        # called, so the over-broad results passed through unfiltered.
        before_filter = len(all_records)
        all_records = self._filter_by_type(all_records)
        if len(all_records) != before_filter:
            _logger.info(
                "Doc-type refine: kept %d/%d (dropped %d off-type)",
                len(all_records), before_filter, before_filter - len(all_records),
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
            raise ScraperExecutionError(
                self.county, "SkagitRecording",
                "could not select doc-type dropdown (search never ran)",
                record_type=self.active_record_type, doc_type=doc_type_label,
                context=str(exc)[:120],
            ) from exc

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
            raise ScraperExecutionError(
                self.county, "SkagitRecording",
                "search click failed (search never ran)",
                record_type=self.active_record_type, doc_type=doc_type_label,
                context=str(exc)[:120],
            ) from exc

        await self.page.wait_for_timeout(5_000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Check result count. The "returned N records" line is Skagit's
        # authoritative signal. Its ABSENCE is NOT a confirmed zero — it can mean a
        # block / silent failure — so classify the page instead of assuming empty.
        body = await self.page.inner_text("body")
        count_match = re.search(r"returned\s+(\d+)\s+records?", body)
        if count_match is None:
            verdict = classify_results_page(
                row_count=0, page_text=body, empty_markers=_EMPTY_MARKERS
            )
            if verdict == "block":
                raise ScraperBlockedError(
                    self.county, "SkagitRecording",
                    f"block wall on results page ({detect_block(body)})",
                    record_type=self.active_record_type, doc_type=doc_type_label,
                    context=body[:120],
                )
            if verdict == "ambiguous":
                raise ScraperExecutionError(
                    self.county, "SkagitRecording",
                    "no result-count line and no empty-marker (search may have "
                    "silently failed / been blocked)",
                    record_type=self.active_record_type, doc_type=doc_type_label,
                    context=body[:120],
                )
            _logger.info("Doc type '%s': genuine empty window", doc_type_label)
            return []
        total = int(count_match.group(1))
        if total == 0:
            return []  # genuine zero for THIS doc type — caller tries the next

        # Extract all pages for this doc type
        all_page_records: list[ScrapedRecord] = []
        page_num = 1
        max_pages = 20

        while page_num <= max_pages:
            # Bounded retry on a transient extraction failure, then fail loud.
            page_records = None
            last_exc: ScraperExecutionError | None = None
            for attempt in range(1, 4):
                try:
                    page_records = await self._extract_page()
                    break
                except ScraperExecutionError as exc:
                    last_exc = exc
                    _logger.warning(
                        "  '%s' page %d extract attempt %d/3 failed: %s",
                        doc_type_label, page_num, attempt, str(exc)[:100],
                    )
                    if attempt < 3:
                        await self.page.wait_for_timeout(1_500 * attempt)
            if page_records is None:
                raise last_exc or ScraperExecutionError(
                    self.county, "SkagitRecording",
                    "page extraction failed after retries (no recorded cause)",
                    record_type=self.active_record_type, doc_type=doc_type_label,
                )
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

        # Canary: the header promised `total` records but we extracted none -> the
        # table parse drifted (or a silent block) — fail loud, don't return [].
        if total > 0 and not all_page_records:
            raise ScraperExecutionError(
                self.county, "SkagitRecording",
                f"results header reported {total} record(s) but extracted 0 rows "
                "(parse drift)",
                record_type=self.active_record_type, doc_type=doc_type_label,
            )

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

                # Death-certificate party orientation via the shared helper, gated
                # to probate scrapes only (the helper also collapses "ESTATE OF"
                # captions, so it must not touch pre_foreclosure/tax/divorce rows).
                # Promotes the decedent over the issuing state/agency; no-op when the
                # grantor is already the decedent. `record.doc_type` carries the
                # Transfer-On-Death signal so a live-owner TOD grantor is preserved.
                if self.active_record_type == "probate" and record.party_name:
                    record.party_name, record.heirs = orient_probate_party(
                        record.party_name, record.heirs, record.doc_type
                    )
                    if not record.party_name:
                        # Guard #2: no decedent on either side. The append gate
                        # below keeps any row with a DATE, so a party-less
                        # probate lead would ship and still be billed (Codex).
                        continue

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
                # No bare-digit fallback: the parcelCell concatenates PLSS /
                # tax-account / permit numbers, so a generic \d{6,} grab stores a
                # WRONG parcel_id (poisoning enrichment + the dedup/property key).
                # Prefer no parcel — require_parcel_id=True drops the row cleanly.

                record.enrichment_data["source"] = "skagit_recording"

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))
        except ScraperExecutionError:
            raise
        except Exception as exc:
            raise ScraperExecutionError(
                self.county, "SkagitRecording", "page extraction failed",
                record_type=self.active_record_type, context=str(exc)[:120],
            ) from exc

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
        """Filter by active record type using both doc_type AND comment fields.

        For pre_foreclosure, additionally drop cancelled/cured/trustee-admin docs
        (Discontinuance, Rescission, Substitution of Trustee — they substring-match
        the legit keywords but are the OPPOSITE of active distress) and re-orient
        the party so the BORROWER (person) lands in party_name. The probate
        death-cert filing-state swap in _extract_page is untouched.
        """
        if not self.active_record_type or self.active_record_type == "all":
            return records
        # Phase B: an explicit selection narrows the refine keyword set to the chosen
        # canonical types (kept in lockstep with the server-label narrowing above).
        if self._refine_keyword_override is not None:
            keywords = self._refine_keyword_override
        else:
            keywords = _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return records
        is_preforeclosure = self.active_record_type == "pre_foreclosure"
        is_divorce = self.active_record_type == "divorce"
        kept = []
        for r in records:
            # Check both doc_type and comment for keyword matches
            text = f"{r.doc_type or ''} {r.enrichment_data.get('comment', '')}".upper()
            if is_divorce:
                # Skagit constrains the server dropdown to "Decree-divorce" (a
                # precise server-side divorce filter), so classify on the DOC TYPE
                # ALONE — NOT the doc_type+comment text (Codex re-review): the
                # classifier's agreement/settlement negatives (SEPARATION AGREEMENT,
                # PROPERTY SETTLEMENT) would otherwise drop a valid Decree-divorce
                # row merely because its comment mentions a settlement. precise=True
                # keeps an ambiguous bare "DISSOLUTION" doc type; the classifier
                # still rejects corporate/entity dissolutions. Then person-guard.
                if not is_divorce_doc(r.doc_type, precise_source=True):
                    continue
                r.party_name, r.heirs = orient_divorce_party(
                    r.party_name, r.heirs, r.doc_type
                )
                kept.append(r)
                continue
            if not any(kw in text for kw in keywords):
                continue
            if is_preforeclosure:
                # Cancelled/cured/trustee-admin docs are not active distress.
                # Check doc_type + comment (Skagit records the real nature in
                # the comment field for otherwise-generic doc types).
                if is_cancellation_or_admin(text):
                    continue
                # Borrower orientation: an NTS is often indexed with the trustee
                # company as grantor and the borrower as grantee. Put the person
                # (homeowner) in party_name; drop bank-vs-trustee records.
                oriented = orient_pre_foreclosure_party(r.party_name, r.heirs)
                if oriented is None:
                    continue
                r.party_name, r.heirs = oriented
            kept.append(r)
        return kept
