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

import html
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

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
from src.scrapers.reliability import ScraperExecutionError, detect_block
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.laserfiche")

_DOC_TYPE_MAP = {
    # Bare "ESTATE" is excluded — it whole-word-matches "REAL ESTATE
    # CONTRACT". "ESTATE OF" only appears on genuine probate filings.
    "probate": [
        "PROBATE", "LETTERS TESTAMENTARY", "LETTERS OF ADMINISTRATION",
        "PERSONAL REPRESENTATIVE", "PERSONAL REP",
        "AFFIDAVIT OF HEIRSHIP", "TRANSFER ON DEATH", "ESTATE OF",
        "WILL", "HEIR",
    ],
    # The publicly RECORDED certificate ("Death Certificate", "Death Certificate to
    # Perfect Death Deed"), as King/Chelan/Clark define the type; never a vital record.
    "death_certificate": ["DEATH CERTIFICATE"],
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


_APN_LOOKUP_KEY = "recording_apn_lookup"
_COUNT_RE = re.compile(r"(\d[\d,]*)\s+Results?\b")
_FIELD_LABEL_RE = re.compile(r"^Rec Doc [A-Za-z ]+$")


def parse_result_count(body: str) -> int | None:
    """The portal's "N Results" count (commas allowed), or None if not rendered."""
    m = _COUNT_RE.search(body or "")
    return int(m.group(1).replace(",", "")) if m else None


def parse_entry_html(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Entry properties from the DocView's ``#metadataTable``: one ``tr.fieldPane`` per
    field, the label in its first cell and one ``div`` per value in the second (a
    multi-valued field keeps every value). {} when the table has not rendered."""
    fields: dict[str, list[str]] = {}
    table = soup.find(id="metadataTable")
    if table is None:
        return fields
    for tr in table.find_all("tr", class_="fieldPane"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        if not _FIELD_LABEL_RE.match(label):
            continue
        values = [d.get_text(" ", strip=True) for d in cells[1].find_all("div", recursive=False)]
        if not values and cells[1].get_text(strip=True):
            values = [cells[1].get_text(" ", strip=True)]
        fields[label] = [v for v in values if v]
    return fields


def entry_render_complete(soup: BeautifulSoup) -> bool:
    """Whether the properties table finished rendering. The viewer renders one empty
    ``tr.fieldPane`` placeholder per field and then that field's labelled row (live
    2026-11-15: an NTS 8 + 8, a death certificate 10 + 10; empty fields are omitted from
    both), so a table with fewer labelled rows than placeholders is still loading."""
    table = soup.find(id="metadataTable")
    if table is None:
        return False
    rows = table.find_all("tr", class_="fieldPane")
    placeholders = sum(1 for tr in rows if not tr.find_all("td", recursive=False))
    labelled = sum(1 for tr in rows if len(tr.find_all("td", recursive=False)) >= 2)
    return labelled > 0 and labelled == placeholders


def entry_contradicts_row(row: dict, fields: dict[str, list[str]]) -> str | None:
    """Why the entry properties cannot be the complete record behind this grid row, or
    None. The grid shows the FIRST value of each field (party names cut to the column
    width), so every value it shows must appear in the properties: a missing one means
    the pane did not finish loading or the portal changed shape."""
    entry_type = (fields.get("Rec Doc Type Desc") or [""])[0].strip().upper()
    row_type = (row.get("doc_type") or "").strip().upper()
    if not row_type or not entry_type.startswith(row_type):
        return "document type differs from the result row"
    parcel = (row.get("parcel") or "").strip()
    if parcel and parcel not in [p.strip() for p in fields.get("Rec Doc Parcel", [])]:
        return f"result-row parcel {parcel!r} missing"
    for side in ("grantor", "grantee"):
        shown = normalize_party_text(row.get(side) or "").upper()
        values = [v.upper() for v in fields.get(f"Rec Doc {side.title()}", [])]
        if shown and not any(v.startswith(shown) or shown.startswith(v) for v in values):
            return f"result-row {side} missing"
    return None


_SURNAME_PARTICLES = frozenset({"DE", "DEL", "DELA", "LA", "LE", "LOS", "LAS", "DI", "DA", "DOS",
                                "DU", "VAN", "VON", "DER", "DEN", "ST", "SAN", "SANTA", "MC", "MAC", "O"})


def _surname_key(name: str) -> str | None:
    """The surname of a LAST FIRST index name, or None when it cannot be told apart
    reliably (a compound or hyphenated surname: "DE LA CRUZ", "SMITH-JONES")."""
    tokens = name.upper().split()
    if len(tokens) < 2:
        return None
    first = tokens[0].rstrip(",.")
    # Hyphenated / apostrophe surnames (SMITH-JONES, O'BRIEN) and particles are uncertain.
    if not re.fullmatch(r"[A-Z]+", first) or first in _SURNAME_PARTICLES:
        return None
    return first


def _primary_party(names: list[str]) -> list[str]:
    """The lead: the index's primary person, plus co-parties sharing that surname. The
    full entry properties list junior lienholders, occupants and agencies beside the
    borrower (live 2026-11-15: an NTS with six person-like grantees, an LP naming
    "SECRETARY OF HOUSING & URBAN DEVELOPMENT"); none of them may be presented as the
    lead. When the surname is uncertain only the primary person is kept."""
    key = _surname_key(names[0])
    if key is None:
        return names[:1]
    return [n for n in names if _surname_key(n) == key]


def read_recorder_pdf(url: str, app_root: str, max_bytes: int, fetch) -> bytes:
    """A recorded document's PDF, from its own recorder only, byte-capped, or ValueError.
    ``fetch(url, path, max_bytes)`` streams it to ``path`` and must abort past the cap
    (safe_download_to_file does)."""
    import os
    import tempfile

    from src.utils.safe_http import same_origin

    # scheme + host + PORT: the session cookie must reach nothing else (Codex 3b P2)
    if not url.startswith("https://") or not same_origin(url, app_root):
        raise ValueError("PDF served from another host")
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        try:
            fetch(url, path, max_bytes)
        except Exception as exc:
            raise ValueError(f"PDF download failed ({type(exc).__name__})") from exc
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    finally:
        os.remove(path)
    if len(data) > max_bytes:
        raise ValueError(f"PDF over {max_bytes} bytes")
    if not data.startswith(b"%PDF-"):
        raise ValueError("download is not a PDF")
    return data


def _nts_kind(doc_type: str) -> str | None:
    """"nts" for a Notice of Trustee's Sale, "nod" for its Discontinuance, else None
    (Cowlitz labels: "Notice Of Trustees Sale", "Discont Of Trustees Sale")."""
    doc = (doc_type or "").upper()
    if "TRUSTEE" not in doc or "SALE" not in doc:
        return None
    if doc.startswith("DISCONT"):
        return "nod"
    return "nts" if doc.startswith("NOTICE") else None


def _afn_base(afn: str) -> str:
    """Grid AFN "3799745-0" -> entry AFN "3799745"."""
    return (afn or "").strip().split("-")[0]


def _is_trustee_sale_notice(doc_type: str) -> bool:
    doc = (doc_type or "").upper()
    return "NOTICE" in doc and "TRUSTEE" in doc and "SALE" in doc


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

    Uses standardized ASP.NET form selectors.
    Parcel IDs are available directly in search results.
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
        require_parcel_id: bool = True,
        doc_types: list[str] | None = None,
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
        # Phase B: narrow the client-side keyword filter to an explicit pre-foreclosure
        # selection (subset of _DOC_TYPE_MAP — registry tokens are an exact partition).
        # None = legacy/full; unmappable/empty raises (fail-closed).
        self._doc_type_keyword_override: list[str] | None = None
        if doc_types is not None and self.active_record_type == "pre_foreclosure":
            from src.scrapers.doc_types import canonical_tokens_or_raise
            self._doc_type_keyword_override = canonical_tokens_or_raise(
                county, state, list(dict.fromkeys(doc_types))
            )

        if host:
            add_scrape_domain(host)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape records from a Laserfiche WebLink portal.

        1. Collect EVERY raw result row of the window, proven complete against the
           portal's "N Results" count (windows that come up short are split).
        2. Keep the rows of the active record type.
        3. Read each kept document's full entry properties: the results grid shows
           only the FIRST grantee / parcel of a multi-valued field.
        """
        _logger.info(
            "Laserfiche WebLink scraper - %s/%s - %s to %s",
            self.county, self.state, date_from, date_to,
        )
        rows = await self._collect(date_from, date_to)
        kept = [r for r in rows if self._type_matches(r["doc_type"])]
        records: list[ScrapedRecord] = []
        for row in kept:
            fields = await self._read_entry(row)
            record = self._build_record(row, fields)
            if record is not None:
                records.append(record)

        if self.require_parcel_id:
            # An ambiguous (multi-parcel) recording is retained with no parcel: it is
            # never delivered or billed (no address), but it is not silently lost.
            records = [
                r for r in records
                if r.parcel_id or r.enrichment_data.get(_APN_LOOKUP_KEY) == "ambiguous"
            ]
        _logger.info(
            "Laserfiche %s %s: %d raw rows, %d of type, %d records",
            self.county, self.active_record_type, len(rows), len(kept), len(records),
        )
        return records

    def _fail(self, reason: str, date_from: str | None = None, date_to: str | None = None):
        return ScraperExecutionError(
            self.county, "Laserfiche WebLink", reason,
            record_type=self.active_record_type, date_from=date_from, date_to=date_to,
        )

    async def _collect(self, date_from: str, date_to: str) -> list[dict]:
        """Every raw row of the window, one search per day, or raise.

        The virtual-scroll grid loses rows on large result sets (live 2026-11-15: 8
        days, 339 of 399; 30 days, 1,048 of 1,428) but a single day reads whole (97/97,
        74/74). A day whose captured rows differ from the portal's own count fails the
        job: a partial day is never delivered as complete.
        """
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")
        if start > end:
            raise self._fail("date_from is after date_to", date_from, date_to)
        days = (end - start).days + 1
        rows: list[dict] = []
        for i in range(days):
            day = (start + timedelta(days=i)).strftime("%m/%d/%Y")
            total, day_rows = await self._search(day, day)
            if len(day_rows) != total:
                raise self._fail(
                    f"captured {len(day_rows)} of {total} rows for {day}", day, day,
                )
            rows.extend(day_rows)
            if self.on_progress:
                self.on_progress(i + 1, days, None, unit="day")
        return rows

    async def _search(self, date_from: str, date_to: str) -> tuple[int, list[dict]]:
        """Run one date-range search; return (portal count, distinct raw rows)."""
        search_url = f"{self.app_root}/CustomSearch.aspx?SearchName=Search&dbid=0&repo=CCIMAGES"
        await self.navigate(search_url)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 -- the date fill below fails loud if the page is not there
            pass
        await self.page.wait_for_timeout(2_000)
        try:
            await self.page.locator("#Search_Input0").fill(date_from)
            await self.page.locator("#Search_Input0_end").fill(date_to)
            await self.page.locator("input[type='submit'][value='Submit']").first.click()
        except Exception as exc:
            # A failed fill/submit means the search never ran; never read it as 0.
            raise self._fail(
                f"search form unusable ({type(exc).__name__}); page never loaded / blocked",
                date_from, date_to,
            ) from exc

        # The "N Results" header and rows load a moment AFTER networkidle settles;
        # poll for the count, which renders for populated AND empty ("0 Results")
        # searches. A one-result search opens that document's viewer instead.
        total = None
        for _ in range(30):
            if "DocView.aspx" in self.page.url:
                return 1, [await self._row_from_open_entry()]
            total = parse_result_count(await self.page.inner_text("body"))
            if total is not None:
                break
            await self.page.wait_for_timeout(1_000)
        if total is None:
            body = await self.page.inner_text("body")
            raise self._fail(
                f"results count never appeared (block marker: {detect_block(body) or 'none'}; "
                f"body {len(body)} chars)", date_from, date_to,
            )
        _logger.info("Search %s-%s returned %d total results", date_from, date_to, total)
        if total == 0:
            return 0, []

        # PrimeFaces virtual-scroll datatable: only ~40 rows are in the DOM at once, so
        # scroll and capture. A row is keyed by its document link (one per entry).
        rows: dict[str, dict] = {}

        async def capture() -> int:
            new = 0
            for r in await self._extract_rows():
                key = r["href"] or f"{r['afn']}|{r['date']}|{r['doc_type']}"
                if key not in rows:
                    rows[key] = r
                    new += 1
            return new

        await self.page.wait_for_timeout(1_500)
        empty_streak = 0
        for _ in range(200):
            new = await capture()
            if len(rows) >= total:
                break
            empty_streak = 0 if new else empty_streak + 1
            if empty_streak >= 8:
                break
            if not await self._advance_scroll():
                # Near the end the remaining distance can be below the recycle
                # threshold, so the last frames never render: oscillate to force them.
                for sweep_top in (-2400, -1600, -800, 0, -2000, -1200, -400):
                    await self._scroll_to_bottom_offset(sweep_top)
                    await self.page.wait_for_timeout(900)
                    await capture()
                    if len(rows) >= total:
                        break
                break
            await self.page.wait_for_timeout(1_200)
        return total, list(rows.values())

    async def _scroll_to_bottom_offset(self, offset: int) -> None:
        await self.page.evaluate("""(offset) => {
            const vt = document.querySelector('.ui-datatable-virtual-table') || document.querySelector('table');
            if (!vt) return;
            let el = vt;
            for (let i = 0; i < 10 && el; i++) {
                const s = window.getComputedStyle(el);
                if ((s.overflowY === 'scroll' || s.overflowY === 'auto') && el.scrollHeight > el.clientHeight) {
                    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight + offset);
                    el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    return;
                }
                el = el.parentElement;
            }
        }""", offset)

    async def _extract_rows(self) -> list[dict]:
        """Raw rows currently in the results table (no filtering).

        Column order (Cowlitz verified): 0=checkbox, 1=AFN (links to the entry's
        DocView), 2=Recording Date, 3=Doc Type, 4=Volume, 5=Page, 6=Grantor,
        7=Grantee, 8=Parcel. Errors propagate: a failed read is not zero rows.
        """
        return await self.page.evaluate(r"""() => {
            const tables = Array.from(document.querySelectorAll('table'));
            let best = null;
            for (const t of tables) {
                const rows = Array.from(t.querySelectorAll('tr'));
                if (rows.length < 3) continue;
                let hasDate = false;
                for (let i = 0; i < Math.min(rows.length, 5); i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length >= 6 && /\d{1,2}\/\d{1,2}\/\d{4}/.test((cells[2]?.textContent || '').trim())) {
                        hasDate = true; break;
                    }
                }
                if (hasDate && (!best || rows.length > best.rows.length)) best = { rows: rows };
            }
            if (!best) return [];
            const out = [];
            for (const row of best.rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 9) continue;
                const vals = Array.from(cells).map(c => (c.textContent || '').trim());
                if (!/\d{1,2}\/\d{1,2}\/\d{4}/.test(vals[2] || '')) continue;
                const a = cells[1].querySelector('a[href]');
                out.push({
                    afn: vals[1] || '', date: vals[2] || '', doc_type: vals[3] || '',
                    volume: vals[4] || '', page_num: vals[5] || '',
                    grantor: (cells[6].innerHTML || '').trim(),
                    grantee: (cells[7].innerHTML || '').trim(),
                    parcel: vals[8] || '',
                    href: a ? a.getAttribute('href') : '',
                });
            }
            return out;
        }""")

    async def _entry_fields(self, afn: str) -> dict[str, list[str]]:
        """Fields of the entry open in the viewer; raise unless the table finished
        rendering (entry_render_complete), two consecutive reads agree, and they name
        ``afn``. Both viewer paths (grid row, one-result search) go through here."""
        fields: dict[str, list[str]] = {}
        previous = None
        for _ in range(20):
            soup = await self.get_soup_async()
            fields = parse_entry_html(soup)
            if (entry_render_complete(soup) and fields.get("Rec Doc AFN")
                    and fields.get("Rec Doc Type Desc") and fields == previous):
                break
            previous = fields
            await self.page.wait_for_timeout(1_000)
        else:
            raise self._fail(f"entry properties for AFN {afn or '?'} never settled")
        got = fields["Rec Doc AFN"][0]
        if afn and got != _afn_base(afn):
            raise self._fail(f"entry properties for AFN {afn} name AFN {got!r}")
        return fields

    async def _row_from_open_entry(self) -> dict:
        """The raw row of a one-result search, read from the viewer it opened."""
        fields = await self._entry_fields("")
        row = {
            "afn": fields["Rec Doc AFN"][0],
            "date": (fields.get("Rec Doc Recording Date") or [""])[0],
            "doc_type": (fields.get("Rec Doc Type Desc") or [""])[0],
            "volume": "", "page_num": "", "grantor": "", "grantee": "", "parcel": "",
            "href": self.page.url,
            "fields": fields,
        }
        if not re.search(r"\d{1,2}/\d{1,2}/\d{4}", row["date"]) or not row["doc_type"]:
            raise self._fail(f"one-result entry {row['afn']} has no date or document type")
        return row

    async def _read_entry(self, row: dict) -> dict[str, list[str]]:
        """Full entry properties of one kept row (all grantees, all parcels)."""
        if row.get("fields"):
            return row["fields"]
        href = row.get("href") or ""
        if "DocView.aspx" not in href:
            raise self._fail(f"result row {row.get('afn') or '?'} has no document link")
        await self.navigate(urljoin(self.app_root + "/", href))
        await self.page.wait_for_timeout(1_000)
        fields = await self._entry_fields(row["afn"])
        problem = entry_contradicts_row(row, fields)
        if problem:
            raise self._fail(f"entry properties for AFN {row['afn']} incomplete: {problem}")
        return fields

    def _build_record(self, row: dict, fields: dict[str, list[str]]) -> ScrapedRecord | None:
        """One lead from a raw row and its full entry properties, or None to drop it."""
        record = ScrapedRecord()
        date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", row.get("date", ""))
        record.date_recorded = date_match.group(1) if date_match else None
        doc_type = (row.get("doc_type") or "").strip()
        record.doc_type = doc_type or None
        # Stacked names keep their " / " boundary through normalize_party_text.
        grantor = normalize_party_text("<br>".join(html.escape(n) for n in fields.get("Rec Doc Grantor", [])))
        grantee = normalize_party_text("<br>".join(html.escape(n) for n in fields.get("Rec Doc Grantee", [])))

        if self.active_record_type == "pre_foreclosure":
            # Cancelled/cured (Discontinuance, Rescission) or pure trustee admin is
            # not active distress.
            if is_cancellation_or_admin(doc_type):
                return None
            if _is_trustee_sale_notice(doc_type):
                # The recorder indexes the TRUSTEE as grantor on a notice of trustee's
                # sale, sometimes an individual attorney: the borrower is a grantee.
                oriented = orient_pre_foreclosure_party(None, grantee)
                if oriented is not None:
                    oriented = (oriented[0], grantor or None)
            else:
                oriented = orient_pre_foreclosure_party(grantor, grantee)
            if oriented is None:
                return None  # no homeowner on either side (bank vs trustee)
            lead = _primary_party([n.strip() for n in oriented[0].split(" / ") if n.strip()])
            record.party_name = " / ".join(lead)
            # Every other indexed party (trustee, lender, title company, other people)
            # stays visible beside the lead, in index order.
            everyone = [n.strip() for side in (grantor, grantee) for n in side.split(" / ") if n.strip()]
            others = [n for n in dict.fromkeys(everyone) if n not in lead]
            record.heirs = " / ".join(others) or None
        elif self.active_record_type in ("probate", "death_certificate"):
            # Agency grantor + decedent grantee (Certificate of Death) is promoted;
            # "Estate of" captions are stripped.
            record.party_name, record.heirs = orient_probate_party(grantor, grantee, doc_type)
            if not record.party_name:
                return None  # a party-less probate lead must never ship or bill
        elif self.active_record_type == "divorce":
            record.party_name, record.heirs = orient_divorce_party(grantor, grantee, doc_type)
        else:
            record.party_name = grantor or None
            record.heirs = grantee or None

        record.enrichment_data = {
            "instrument_number": row.get("afn", "").strip(),
            "source": "laserfiche_weblink",
        }
        parcels = list(dict.fromkeys(p.strip() for p in fields.get("Rec Doc Parcel", []) if p.strip()))
        if len(parcels) == 1:
            record.parcel_id = parcels[0]
            record.enrichment_data[_APN_LOOKUP_KEY] = "found"
        elif parcels:
            # Several parcels on one recording: never pick one (it could attach the
            # wrong property); hold the row with its candidates instead.
            record.enrichment_data[_APN_LOOKUP_KEY] = "ambiguous"
            record.enrichment_data["apn_candidates"] = parcels
        else:
            record.enrichment_data[_APN_LOOKUP_KEY] = "none"
        related = [a.strip() for a in fields.get("Rec Doc Related AFN", []) if a.strip()]
        if related:
            record.enrichment_data["related_afns"] = related
        code = (fields.get("Rec Doc Document Type") or [""])[0].strip()
        if code:
            record.enrichment_data["doc_type_code"] = code
        # Volume/Page is the recorder's book/page, never a legal description.
        vol, pg = row.get("volume", "").strip(), row.get("page_num", "").strip()
        if vol or pg:
            record.enrichment_data["volume_page"] = f"Vol {vol} Pg {pg}".strip()
        record.raw_html_hash = self.make_hash(record.to_dict())
        return record

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

    # ── NTS crawler support (nts_crawler.crawl_nts_cowlitz_recorder) ─────────────
    async def discover(self, date_from: str, date_to: str) -> list[dict]:
        """Every recorded Notice of Trustee's Sale and Discontinuance of Trustee's Sale in
        the window, from the same complete per-day read as scrape(); each with its entry
        properties: {item_id, entry, instrument, doc_code, record_date, recording_apns,
        related, href}. ``entry`` is the full grid name ("3799745-0"): the entry id is not
        stable (the repository re-indexed 2026-11-17), the name is. doc_code is the recorder's code ("NTTRUS"), except a discontinuance is
        "NOD", the staging table's code for one. Call inside ``async with``."""
        rows = await self._collect(date_from, date_to)
        found: list[dict] = []
        for row in rows:
            kind = _nts_kind(row["doc_type"])
            if kind is None:
                continue
            fields = await self._read_entry(row)
            code = (fields.get("Rec Doc Document Type") or [""])[0].strip().upper()
            if (kind == "nod") != (code == "DISTRS") or (kind == "nts") != (code == "NTTRUS"):
                raise self._fail(f"AFN {row['afn']}: type {row['doc_type']!r} carries code {code!r}")
            m = re.search(r"[?&]id=(\d+)", row.get("href") or "")
            if not m:
                raise self._fail(f"AFN {row['afn']}: no document id")
            found.append({
                "item_id": m.group(1),
                "entry": row["afn"].strip(),
                "instrument": _afn_base(row["afn"]),
                "doc_code": "NOD" if kind == "nod" else code,
                "record_date": datetime.strptime(row["date"], "%m/%d/%Y").strftime("%m/%d/%Y"),
                "recording_apns": list(dict.fromkeys(
                    p.strip() for p in fields.get("Rec Doc Parcel", []) if p.strip())),
                "related": [a.strip() for a in fields.get("Rec Doc Related AFN", []) if a.strip()],
                "href": row["href"],
            })
        return found

    async def fetch_pdf(self, doc: dict, max_bytes: int) -> bytes:
        """The recorded document as the viewer's free "Print (as PDF)" serves it (no
        watermark). The viewer generates it at a session-bound URL (/PDF10/<id>/<doc>)
        and then loads it: a headless browser downloads it, a headed one (production
        runs Chromium under Xvfb) opens it in its PDF viewer and fires no download
        (first production crawl, 2026-11-16: 11 of 11 timed out). So the URL is taken
        from the request itself, either way; any download is cancelled and any viewer
        tab closed, and the URL is re-read through safe_download_to_file, so the byte
        cap and time limit hold WHILE it streams (Codex 3b P2). Raises on any failure:
        never an empty document."""
        import asyncio

        from src.utils.safe_http import safe_download_to_file, same_origin

        afn = doc["instrument"]
        await self.navigate(urljoin(self.app_root + "/", doc["href"]))
        await self._entry_fields(afn)
        context = self.page.context
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _take_pdf_url(route) -> None:
            # The browser never fetches the document itself (no uncapped download, no
            # viewer): its URL is all we need, and the capped refetch below reads it.
            # Aborted first, published after (Codex): cleanup cannot race the abort.
            url = route.request.url
            try:
                await route.abort()
            except Exception as exc:
                if not found.done():
                    found.set_exception(exc)
                return
            if not found.done():
                found.set_result(url)

        await context.route("**/PDF10/**", _take_pdf_url)
        try:
            try:
                await self.page.locator("#STR_DOWNLOAD_PDF").click()
                await self.page.get_by_role("button", name="Download & Print").click()
                url = await asyncio.wait_for(found, timeout=120)
            except Exception as exc:
                raise self._fail(f"AFN {afn}: PDF not served ({type(exc).__name__})") from exc
            finally:
                # Viewer tabs close while interception still stands, so no load they
                # retry can reach the document uncapped (Codex).
                for other in [p for p in context.pages if p is not self.page]:
                    await other.close()
        finally:
            await context.unroute("**/PDF10/**", _take_pdf_url)
        if not same_origin(url, self.app_root):  # checked before any cookie is read
            raise self._fail(f"AFN {afn}: PDF served from another host")
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in await self.page.context.cookies(url))
        headers = {"Cookie": cookies, "User-Agent": await self.page.evaluate("navigator.userAgent")}

        def _fetch(u: str, path: str, cap: int) -> None:
            safe_download_to_file(u, path, max_bytes=cap, require_https=True, follow_redirects=False,
                                  headers=headers, timeout=60, deadline_s=120)

        try:
            return await asyncio.to_thread(read_recorder_pdf, url, self.app_root, max_bytes, _fetch)
        except ValueError as exc:
            raise self._fail(f"AFN {afn}: {exc}") from exc

    def _type_matches(self, doc_type: str) -> bool:
        """Whether a raw row's document type belongs to the active record type."""
        if not self.active_record_type or self.active_record_type == "all":
            return True
        keywords = self._doc_type_keyword_override or _DOC_TYPE_MAP.get(self.active_record_type, [])
        if not keywords:
            return True
        if self.active_record_type == "divorce":
            # Fail closed on ambiguous bare "DISSOLUTION" (corporate dissolutions).
            return is_divorce_doc(doc_type, precise_source=False)
        return _doc_type_matches((doc_type or "").upper(), keywords)
