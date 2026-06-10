"""Laserfiche WebLink template scraper for county recorder portals.

Covers counties using Laserfiche WebLink 10 for public record search.
First confirmed: Cowlitz County, WA.

Laserfiche WebLink sites share:
- Welcome page at /WLAudPublic/welcome.aspx (no disclaimer, just entry links)
- Custom search form at /CustomSearch.aspx with numbered inputs:
  - Search_Input0 + Search_Input0_end: recording date range
  - Search_Input11: document type filter (optional)
  - Submit button triggers ASP.NET postback
- Results table with columns:
  AFN, Recording Date, Doc Type Desc, Volume, Page, Grantor, Grantee, Parcel
- Parcel IDs are available IN the search results (unlike Tyler SelfService)
- Pagination via ASP.NET __doPostBack pager links

Volume reference (Cowlitz 2026-04-12):
- 7-day total: 475 records (all types)
"""

import re

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import (
    BridgeScraper,
    ScrapedRecord,
    normalize_party_text,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.laserfiche")

_DOC_TYPE_MAP = {
    # Bare "ESTATE" is excluded — it whole-word-matches "REAL ESTATE
    # CONTRACT". "ESTATE OF" only appears on genuine probate filings.
    "probate": [
        "PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE", "PERSONAL REP", "DEATH CERTIFICATE",
        "AFFIDAVIT OF HEIRSHIP", "TRANSFER ON DEATH", "ESTATE OF",
        "WILL", "HEIR",
    ],
    "pre_foreclosure": [
        "LIS PENDENS", "NOTICE OF TRUSTEE", "TRUSTEE SALE",
        "TRUSTEE'S SALE", "NOTICE OF DEFAULT", "FORECLOSURE",
    ],
    "tax_delinquent": [
        "TAX LIEN", "CERTIFICATE OF DELINQUENCY", "TAX DELINQUENT",
        "CERTIFICATE OF SALE", "TREASURER",
    ],
    "divorce": [
        "DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION",
    ],
}


def _doc_type_matches(doc_type: str, keywords: list[str]) -> bool:
    """Match doc_type against keywords without substring false-positives.

    Multi-word keywords use substring match; single-word keywords must
    match as a whole word so "WILL" doesn't leak "GOODWILL" into probate
    and "NTS"-style abbreviations don't leak "COVENANTS" into
    pre_foreclosure.
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


class LaserficheWebLinkScraper(BridgeScraper):
    """Template scraper for Laserfiche WebLink recorder portals.

    Zero Claude AI cost — uses standardized ASP.NET form selectors.
    Parcel IDs are available directly in search results.
    """

    def __init__(
        self,
        base_url: str,
        county: str,
        state: str,
        record_types: list[str] | None = None,
        record_type: str | None = None,
        require_parcel_id: bool = True,
    ):
        super().__init__()
        # Normalise base_url to the WLAudPublic root
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        # Extract the app path (e.g. /WLAudPublic or /WLAUDPublic)
        path_parts = parsed.path.strip("/").split("/")
        app_path = path_parts[0] if path_parts else "WLAudPublic"
        self.app_root = f"{scheme}://{host}/{app_path}"
        self.base_url = base_url
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)
        self.require_parcel_id = require_parcel_id

        if host:
            add_scrape_domain(host)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from a Laserfiche WebLink portal."""

        _logger.info(
            "Laserfiche WebLink scraper - %s/%s - %s to %s",
            self.county, self.state, date_from, date_to,
        )

        # Navigate to the search page
        search_url = f"{self.app_root}/CustomSearch.aspx?SearchName=Search&dbid=0&repo=CCIMAGES"
        await self.navigate(search_url)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await self.page.wait_for_timeout(2_000)

        # Fill date range
        _logger.info("Filling dates: %s to %s", date_from, date_to)
        try:
            await self.page.locator("#Search_Input0").fill(date_from)
            await self.page.locator("#Search_Input0_end").fill(date_to)
        except Exception as exc:
            _logger.error("Failed to fill dates: %s", str(exc)[:120])
            return []

        # Submit search
        try:
            await self.page.locator("input[type='submit'][value='Submit']").click()
            await self.page.wait_for_timeout(3_000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        except Exception as exc:
            _logger.error("Submit failed: %s", str(exc)[:120])
            return []

        # Wait for the async PrimeFaces datatable to populate, THEN read the
        # count. The "N Results" header and the rows load a moment AFTER
        # networkidle settles, so a single immediate read catches the
        # datatable's transient "No records found" placeholder and wrongly
        # bails with total=0 (this silently zeroed every Laserfiche county —
        # e.g. Cowlitz, which actually has thousands of records in-window).
        # Poll for the count text, which appears for both populated AND
        # genuinely-empty ("0 Results") searches.
        total = 0
        for _ in range(30):  # up to ~30s
            body = await self.page.inner_text("body")
            count_match = re.search(r"(\d+)\s+Results?", body)
            if count_match:
                total = int(count_match.group(1))
                break
            await self.page.wait_for_timeout(1_000)
        _logger.info("Search returned %d total results", total)
        if total == 0:
            return []

        # Laserfiche WebLink uses PrimeFaces virtual-scroll datatable —
        # only ~40 rows are in the DOM at once. We incrementally scroll
        # the results container and capture visible rows at each
        # position. Dedupe is by AFN (instrument number) so rows
        # that stay in the DOM across scrolls aren't counted twice.
        all_records: list[ScrapedRecord] = []
        seen_afns: set[str] = set()
        seen_hashes: set[str] = set()
        empty_scroll_streak = 0
        max_scrolls = 200

        for scroll_idx in range(max_scrolls):
            page_records = await self._extract_page()
            new = 0
            for r in page_records:
                afn = (r.enrichment_data or {}).get("instrument_number", "") if r.enrichment_data else ""
                key = afn or self.make_hash(r.to_dict())
                if key in seen_afns or key in seen_hashes:
                    continue
                seen_afns.add(key)
                h = self.make_hash(r.to_dict())
                seen_hashes.add(h)
                r.raw_html_hash = h
                all_records.append(r)
                new += 1

            if len(all_records) >= total:
                _logger.info("Scroll %d: captured %d/%d — done", scroll_idx, len(all_records), total)
                break

            if new == 0:
                empty_scroll_streak += 1
                # PrimeFaces only recycles rows every ~1.5 viewports of
                # scroll, so several consecutive 800px scrolls can
                # return 0 new rows before the DOM advances. Tolerate
                # up to 8 empty scrolls before giving up.
                if empty_scroll_streak >= 8:
                    _logger.info(
                        "Scroll %d: no new rows after 8 attempts — stopping at %d/%d",
                        scroll_idx, len(all_records), total,
                    )
                    break
            else:
                empty_scroll_streak = 0

            if scroll_idx % 5 == 0:
                _logger.info("Scroll %d: %d new (total: %d/%d)", scroll_idx, new, len(all_records), total)

            # Advance the virtual scroll viewport. Scroll by roughly a
            # viewport-height less than the full client height so we
            # overlap by one row (guards against the top/bottom row
            # being cut off during virtual-row recycling).
            advanced = await self._advance_scroll()
            if not advanced:
                _logger.info("Scroll %d: reached bottom at %d/%d — sweeping", scroll_idx, len(all_records), total)
                # Bottom sweep: near the end the remaining scroll
                # distance can be smaller than PrimeFaces's virtual
                # recycle threshold, so the final frames never render.
                # Oscillate up/down in 400px steps to force the widget
                # to re-render rows we may have skipped.
                for sweep_top in (-2400, -1600, -800, 0, -2000, -1200, -400):
                    await self.page.evaluate(f"""() => {{
                        const vt = document.querySelector('.ui-datatable-virtual-table') || document.querySelector('table');
                        if (!vt) return;
                        let el = vt;
                        for (let i = 0; i < 10 && el; i++) {{
                            const s = window.getComputedStyle(el);
                            if ((s.overflowY === 'scroll' || s.overflowY === 'auto') && el.scrollHeight > el.clientHeight) {{
                                const max = el.scrollHeight - el.clientHeight;
                                el.scrollTop = Math.max(0, max + ({sweep_top}));
                                el.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
                                return;
                            }}
                            el = el.parentElement;
                        }}
                    }}""")
                    await self.page.wait_for_timeout(900)
                    sweep_records = await self._extract_page()
                    for r in sweep_records:
                        afn = (r.enrichment_data or {}).get("instrument_number", "") if r.enrichment_data else ""
                        key = afn or self.make_hash(r.to_dict())
                        if key in seen_afns:
                            continue
                        seen_afns.add(key)
                        h = self.make_hash(r.to_dict())
                        r.raw_html_hash = h
                        all_records.append(r)
                    if len(all_records) >= total:
                        break
                _logger.info("Bottom sweep complete: %d/%d", len(all_records), total)
                break
            await self.page.wait_for_timeout(1_200)

        # Filter by active record type
        filtered = self._filter_by_type(all_records)
        _logger.info(
            "Laserfiche %s: %d/%d after %s filter",
            self.county, len(filtered), len(all_records), self.active_record_type or "none",
        )

        # Drop records without parcel_id if required
        if self.require_parcel_id:
            before = len(filtered)
            filtered = [r for r in filtered if r.parcel_id]
            dropped = before - len(filtered)
            if dropped:
                _logger.info("Dropped %d/%d records with no parcel_id", dropped, before)

        return filtered

    async def _extract_page(self) -> list[ScrapedRecord]:
        """Extract records from the current results table.

        Laserfiche WebLink renders results in a standard HTML table.
        Column order (Cowlitz verified):
        0=checkbox, 1=AFN, 2=Recording Date, 3=Doc Type, 4=Volume,
        5=Page, 6=Grantor, 7=Grantee, 8=Parcel
        """
        records: list[ScrapedRecord] = []
        try:
            raw = await self.page.evaluate("""() => {
                // Find the results table — pick the table with the most
                // rows where at least one row has a date (M/D/YYYY) in
                // the 3rd cell. Laserfiche WebLink doesn't always include
                // recognisable header text in the results table itself.
                const tables = Array.from(document.querySelectorAll('table'));
                let best = null;
                for (const t of tables) {
                    const rows = Array.from(t.querySelectorAll('tr'));
                    if (rows.length < 3) continue;
                    // Check if any row has a date pattern in cell[2]
                    let hasDate = false;
                    for (let i = 0; i < Math.min(rows.length, 5); i++) {
                        const cells = rows[i].querySelectorAll('td');
                        if (cells.length >= 6) {
                            const val = (cells[2]?.textContent || '').trim();
                            if (/\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(val)) { hasDate = true; break; }
                        }
                    }
                    if (hasDate && (!best || rows.length > best.rows.length)) {
                        best = { table: t, rows: rows };
                    }
                }
                if (!best) return [];

                const out = [];
                // Skip header row(s) — find first row with a date pattern
                for (const row of best.rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 6) continue;
                    // Extract text from each cell
                    const vals = Array.from(cells).map(c => (c.textContent || '').trim());
                    // Party cells (grantor/grantee) can stack multiple co-owners
                    // separated by structural markup (<br>, nameSeperator). Read
                    // their innerHTML so those separators survive into
                    // normalize_party_text() on the Python side — textContent
                    // would collapse "OWNER A" + "OWNER B" into one token.
                    const htmlOf = (i) => cells[i] ? (cells[i].innerHTML || '').trim() : '';
                    // Look for a date in position 2 (MM/DD/YYYY or M/D/YYYY)
                    const dateVal = vals[2] || '';
                    if (!/\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(dateVal)) continue;
                    out.push({
                        afn: vals[1] || '',
                        date: dateVal,
                        doc_type: vals[3] || '',
                        volume: vals[4] || '',
                        page_num: vals[5] || '',
                        grantor: htmlOf(6),
                        grantee: htmlOf(7),
                        parcel: vals[8] || '',
                    });
                }
                return out;
            }""")

            for item in raw:
                record = ScrapedRecord()
                # Date
                date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", item.get("date", ""))
                if date_match:
                    record.date_recorded = date_match.group(1)
                # Doc type
                doc_type = item.get("doc_type", "").strip()
                record.doc_type = doc_type if doc_type else None
                # Grantor → party_name (HTML from the cell — normalize so
                # stacked co-owners keep their " / " boundary).
                grantor = normalize_party_text(item.get("grantor", ""))
                if grantor:
                    record.party_name = grantor
                # Grantee → heirs
                grantee = normalize_party_text(item.get("grantee", ""))
                if grantee:
                    record.heirs = grantee
                # Parcel
                parcel = item.get("parcel", "").strip()
                if parcel and re.match(r"[\d\w]", parcel):
                    record.parcel_id = parcel
                # Instrument number
                afn = item.get("afn", "").strip()
                record.enrichment_data = {
                    "instrument_number": afn,
                    "source": "laserfiche_weblink",
                }
                # Legal description from volume/page
                vol = item.get("volume", "").strip()
                pg = item.get("page_num", "").strip()
                if vol or pg:
                    record.legal_description = f"Vol {vol} Pg {pg}".strip()

                if record.party_name or record.date_recorded:
                    records.append(record)

            _logger.info("Extracted %d records from page", len(records))
        except Exception as exc:
            _logger.warning("Extraction error: %s", str(exc)[:120])

        return records

    async def _advance_scroll(self) -> bool:
        """Advance the virtual-scroll results container by one viewport.

        Laserfiche WebLink uses a PrimeFaces datatable with virtual
        scrolling (`.ui-datatable-scrollable-body`). Only the rows in
        the current viewport are in the DOM; scrolling triggers the
        widget to render new rows on top/below. Returns False when the
        scroll container is already at the bottom.
        """
        try:
            result = await self.page.evaluate("""() => {
                // Locate the scrollable body by climbing up from the
                // virtual table. The container has overflow-y set to
                // 'auto' or 'scroll' and is recognisable by the
                // 'ui-datatable-scrollable-body' class on Laserfiche.
                const vtable = document.querySelector('.ui-datatable-virtual-table')
                    || document.querySelector('table');
                if (!vtable) return { ok: false, reason: 'no-table' };
                let el = vtable;
                let container = null;
                for (let i = 0; i < 10 && el; i++) {
                    const style = window.getComputedStyle(el);
                    if (style.overflowY === 'scroll' || style.overflowY === 'auto') {
                        if (el.scrollHeight > el.clientHeight) {
                            container = el;
                            break;
                        }
                    }
                    el = el.parentElement;
                }
                if (!container) return { ok: false, reason: 'no-container' };
                const before = container.scrollTop;
                const max = container.scrollHeight - container.clientHeight;
                if (before >= max - 1) return { ok: false, reason: 'at-bottom', scrollTop: before, max };
                // Step by ~20 rows (~800px at 40px/row). Small enough
                // to not skip past the virtual recycle window, large
                // enough to reliably trigger PrimeFaces to re-render
                // rows once the cumulative scroll crosses its
                // recycle threshold.
                const step = 800;
                container.scrollTop = Math.min(before + step, max);
                // Fire a scroll event — PrimeFaces listens for it.
                container.dispatchEvent(new Event('scroll', { bubbles: true }));
                return { ok: true, scrollTop: container.scrollTop, max };
            }""")
            return bool(result and result.get('ok'))
        except Exception as exc:
            _logger.info("Scroll advance failed: %s", str(exc)[:80])
            return False

    def _filter_by_type(self, records: list[ScrapedRecord]) -> list[ScrapedRecord]:
        """Keep only records whose doc_type matches the active record type."""
        if not self.active_record_type or self.active_record_type == "all":
            return records
        keywords = _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return records
        kept = []
        for r in records:
            doc = (r.doc_type or "").upper()
            if _doc_type_matches(doc, keywords):
                kept.append(r)
        return kept
