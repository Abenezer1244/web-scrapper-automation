"""Whatcom County (WA) — Helion "Digital Research Room" recording portal.

Portal: https://recording.whatcomcounty.us/
Platform: Helion Digital Research Room (ASP.NET MVC)

Flow:
1. Navigate to home → click "I Agree" disclaimer
2. Navigate to /?mode=Advanced (advanced search form)
3. Fill RecordingDateStart / RecordingDateEnd
4. Click Search
5. Parse .search-result div cards directly — APN is embedded in the
   card text as "APN# 4003175334900000" (16 plain digits, matches WA
   statewide ORIG_PARCEL_ID directly, no formatter needed)
6. Filter by doc-type keyword client-side (same approach as Clark)
7. Paginate via "Next 50" link

Whatcom's result cards include GRANTOR, GRANTEE, Doc Type, date, and
APN all inline — no HTTP detail fetch needed, unlike Thurston/Spokane.
"""

import re
from datetime import datetime, timedelta

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.probate import orient_probate_party
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.whatcom_wa")

_BASE_URL = "https://recording.whatcomcounty.us/"
_ADVANCED_URL = "https://recording.whatcomcounty.us/?mode=Advanced"

# Whatcom uses the same doc-type terminology as Clark's LandmarkWeb
_DOC_TYPE_KEYWORDS = {
    "probate": [
        "DEATH CERTIFICATE", "LACK OF PROBATE", "TRANSFER ON DEATH",
        "WILL", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE", "AFFIDAVIT OF HEIRSHIP",
    ],
    "pre_foreclosure": [
        "NOTICE OF TRUSTEE SALE", "LIS PENDENS", "NOTICE OF DEFAULT",
        "NOTICE OF FORECLOSURE", "FORECLOSURE",
    ],
    "tax_delinquent": [
        "TAX LIEN", "CERTIFICATE OF DELINQUENCY", "CERTIFICATE OF SALE",
        "FEDERAL TAX LIEN",
    ],
    "divorce": ["DISSOLUTION", "DIVORCE"],
}

add_scrape_domain("recording.whatcomcounty.us")


class WhatcomWAScraper(BridgeScraper):
    """Whatcom County Helion recording portal scraper."""

    def __init__(self, record_type: str = "probate"):
        super().__init__()
        self._record_type = record_type
        self._keywords = [
            k.upper() for k in _DOC_TYPE_KEYWORDS.get(record_type, _DOC_TYPE_KEYWORDS["probate"])
        ]

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        chunk_days = 30

        _logger.info(
            "Whatcom WA %s — %s to %s",
            self._record_type, date_from, date_to,
        )

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
                chunk_records = await self._search_chunk_all_pages(cf, ct)
            except Exception as exc:
                _logger.warning("Chunk failed: %s — skipping", str(exc)[:80])
                chunk_start = chunk_end
                continue

            new_count = 0
            for rec in chunk_records:
                h = self.make_hash(rec.to_dict())
                if h not in seen:
                    seen.add(h)
                    rec.raw_html_hash = h
                    all_records.append(rec)
                    new_count += 1

            _logger.info("Chunk done: %d new (total %d)", new_count, len(all_records))
            if self.on_progress:
                self.on_progress(0, 0, len(all_records))

            chunk_start = chunk_end

        _logger.info("Whatcom WA %s complete — %d records", self._record_type, len(all_records))
        return all_records

    async def _accept_disclaimer(self) -> None:
        """Click the 'I Agree' button on the disclaimer page."""
        try:
            await self.page.wait_for_timeout(1500)
            btn = self.page.locator(
                "button:has-text('I Agree'), button:has-text('Agree'), "
                "input[type='submit'][value*='Agree' i]"
            )
            if await btn.count() > 0:
                try:
                    async with self.page.expect_navigation(timeout=10_000):
                        await btn.first.click()
                except Exception:
                    await self.page.wait_for_timeout(2000)
                _logger.info("Disclaimer accepted, now at: %s", self.page.url)
            else:
                _logger.info("No disclaimer button found — continuing")
        except Exception as exc:
            _logger.info("Disclaimer handling: %s", str(exc)[:80])

    async def _search_chunk_all_pages(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run a date-range search and paginate through all result pages."""
        # Navigate to advanced search form for each chunk
        await self.page.goto(_ADVANCED_URL, wait_until="domcontentloaded", timeout=30_000)
        await self.page.wait_for_timeout(1500)

        # Fill dates
        start_el = self.page.locator("#Criteria_Filter_RecordingDateStart")
        end_el = self.page.locator("#Criteria_Filter_RecordingDateEnd")
        await start_el.fill(date_from)
        await end_el.fill(date_to)
        _logger.info("Dates: %s to %s", date_from, date_to)

        # Submit
        search_btn = self.page.locator(
            "button:has-text('Search'), input[type='submit'][value*='Search' i]"
        )
        try:
            async with self.page.expect_navigation(timeout=30_000):
                await search_btn.first.click()
        except Exception:
            await self.page.wait_for_timeout(5000)

        # Wait for result cards or "no results" message
        try:
            await self.page.wait_for_selector(
                ".search-result, .floating-message, #search-results-header",
                timeout=20_000,
            )
        except Exception:
            _logger.warning("Result container did not appear in time")

        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        seen_raw_hashes: set[str] = set()
        max_pages = 50

        for page_num in range(1, max_pages + 1):
            await self.page.wait_for_timeout(1500)
            page_records, raw_texts = await self._extract_page()

            new_count = 0
            for rec in page_records:
                h = self.make_hash(rec.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_records.append(rec)
                    new_count += 1

            # Pagination-failure detection. Previously we broke when
            # new_count == 0, but that conflates "the next-page click
            # didn't advance" with "all 50 records on this page failed
            # the doc-type filter". For counties like Whatcom where
            # Helion returns unfiltered results and probate filings
            # are sparse, the second case happens constantly — and
            # bailing on it meant we never saw probates that lived
            # past page 2. Check whether the raw card set is actually
            # new (any new fingerprint = a real next page) instead.
            new_raw_count = 0
            for raw_text in raw_texts:
                import hashlib
                rh = hashlib.sha1(raw_text.encode("utf-8"), usedforsecurity=False).hexdigest()
                if rh not in seen_raw_hashes:
                    seen_raw_hashes.add(rh)
                    new_raw_count += 1

            _logger.info(
                "Page %d — %d new records, %d new raw cards (chunk total %d)",
                page_num, new_count, new_raw_count, len(all_records),
            )
            if new_raw_count == 0 and page_num > 1:
                _logger.info("  Pagination stalled (0 new raw cards) — stopping")
                break

            if not await self._go_next_page():
                break

        return all_records

    async def _extract_page(self) -> tuple[list[ScrapedRecord], list[str]]:
        """Extract records from the current results page.

        Returns ``(kept_records, raw_texts)``. ``raw_texts`` is the full
        set of card contents on this page — the caller uses it to
        detect pagination failure (identical cards across pages) vs a
        page whose records were all filtered out by the doc-type
        allowlist.
        """
        raw = await self.page.evaluate("""
            (() => {
                const cards = document.querySelectorAll('.search-result');
                const out = [];
                for (const card of cards) {
                    const text = (card.innerText || card.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text) continue;
                    out.push(text);
                }
                return out;
            })()
        """)

        if not raw:
            _logger.info("No .search-result cards on this page")
            return [], []

        _logger.info("Raw cards on page: %d", len(raw))

        records: list[ScrapedRecord] = []
        dropped_wrong_doctype = 0
        dropped_no_apn = 0
        doc_type_counter: dict[str, int] = {}

        for text in raw:
            # Doc type
            dt_match = re.search(r"Doc Type:\s*([^\n]+?)(?:\s+GRANTOR|\s+APN|$)", text, re.IGNORECASE)
            doc_type = dt_match.group(1).strip() if dt_match else ""
            doc_type_upper = doc_type.upper()
            doc_type_counter[doc_type] = doc_type_counter.get(doc_type, 0) + 1

            # Filter by record type keywords client-side
            matched = False
            for kw in self._keywords:
                if kw and kw in doc_type_upper:
                    matched = True
                    break
            if not matched:
                dropped_wrong_doctype += 1
                continue

            # APN# — 16 digits is the Whatcom canonical format
            apn_match = re.search(r"APN#\s*(\d{10,})", text)
            if not apn_match:
                dropped_no_apn += 1
                continue
            parcel_id = apn_match.group(1)

            # Date Recorded
            date_match = re.search(r"Date Recorded:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
            date_recorded = date_match.group(1) if date_match else ""

            # Instrument number (first token, pattern YYYY-NNNNNNNN)
            instr_match = re.search(r"\b(\d{4}-\d{5,})\b", text)
            instrument = instr_match.group(1) if instr_match else ""

            # Grantor — up to the next field keyword
            grantor_match = re.search(
                r"GRANTOR:\s*(.+?)\s+(?:GRANTEE:|APN#|Doc Type:|$)",
                text, re.IGNORECASE,
            )
            grantor = grantor_match.group(1).strip().rstrip(",").rstrip(";") if grantor_match else ""

            # Grantee — up to the next field or end
            grantee_match = re.search(
                r"GRANTEE:\s*(.+?)\s+(?:GRANTOR:|APN#|Doc Type:|$)",
                text, re.IGNORECASE,
            )
            grantee = grantee_match.group(1).strip().rstrip(",").rstrip(";") if grantee_match else ""

            record = ScrapedRecord()
            record.parcel_id = parcel_id
            if self._record_type == "probate":
                # Death certs index the issuing agency (WA Dept of Health) /
                # filing state as grantor, with the DECEDENT as grantee; promote
                # the decedent and strip "Estate of" captions. No-op when the
                # grantor is already the decedent.
                record.party_name, record.heirs = orient_probate_party(
                    grantor, grantee, doc_type
                )
            else:
                record.party_name = grantor
                record.heirs = grantee
            record.doc_type = doc_type
            record.date_recorded = date_recorded
            record.legal_description = instrument
            record.enrichment_data = {
                "source": "whatcom_county_recorder",
                "instrument_number": instrument,
                "doc_type": doc_type,
                "parcel_id": parcel_id,
            }

            if record.party_name or record.date_recorded:
                records.append(record)

        _logger.info(
            "Records kept: %d / %d (dropped_wrong_doctype=%d, dropped_no_apn=%d)",
            len(records), len(raw), dropped_wrong_doctype, dropped_no_apn,
        )
        top_types = sorted(doc_type_counter.items(), key=lambda kv: -kv[1])[:5]
        if top_types:
            _logger.info(
                "  Top doc types on page: %s",
                ", ".join(f"{t}={n}" for t, n in top_types),
            )
        return records, raw

    async def _go_next_page(self) -> bool:
        """Click the 'Next 50' pagination button if enabled."""
        try:
            next_btn = self.page.locator(
                "a:has-text('Next'), button:has-text('Next'), "
                ".pagination .next a, [aria-label='Next']"
            )
            if await next_btn.count() == 0:
                return False
            first = next_btn.first
            disabled = await first.get_attribute("disabled")
            cls = await first.get_attribute("class") or ""
            if disabled or "disabled" in cls.lower():
                return False
            await first.click()
            await self.page.wait_for_timeout(2500)
            return True
        except Exception as exc:
            _logger.info("Next page click failed: %s", str(exc)[:80])
            return False
