"""iDocMarket template scraper for Tyler Technologies' iDocMarket recorder portals.

iDocMarket (idocmarket.com) is a Tyler Technologies platform that hosts online
recorded-document search for many small counties. The per-county "Basic Search"
is public (no login, no subscription) — a subscription is only required to VIEW
or PRINT the document image, not to search the index. We only read the index
(doc type, date, doc number, grantor/grantee, first legal), so no credentials
are needed and none are stored (project rule).

WA counties using iDocMarket:
    Columbia (COLWA1) — the only WA county on the platform.

The per-county search page is a server-rendered ASP.NET MVC form:
- `#StartRecordDate` / `#EndRecordDate` text inputs (MM/DD/YYYY)
- an `input[type=submit]` "Search" button (anti-CSRF __RequestVerificationToken
  is a hidden field on the form — we submit the real form, never a raw POST)
- results render back on the same URL as `div.row.result-item` cards:
    h4.doc-title[sort-desc=instrument]   -> document type
    span[sort-desc=docnum]               -> "#51362"
    .col-md-1 span:first                 -> record date (M/D/YYYY)
    .full-parties .grantor-line .party-value  -> grantor(s)
    .full-parties .grantee-line .party-value  -> grantee(s)
    .first-legals span:last               -> first legal (PLSS T/R/S — no APN)

There is no parcel/APN in the index (only a PLSS legal), so records are
parcel-less — consistent with the EagleWeb parcel-less counties (island,
pacific). Production enrichment fills addresses downstream where possible.

base_url for an iDocMarket connector is the FULL per-county search URL
(e.g. https://www.idocmarket.com/COLWA1/Document/Search). The county code lives
in the URL, so this template is generic across iDocMarket counties.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import (
    BridgeScraper,
    ScrapedRecord,
    normalize_party_text,
)
from src.scrapers.preforeclosure import (
    is_cancellation_or_admin,
    orient_pre_foreclosure_party,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.template.idocmarket")

# Tokenize a doc-type string on runs of letters/digits so possessives and
# slashes split cleanly: "ADMINISTRATOR'S DEED" -> {ADMINISTRATOR, S, DEED},
# "AFFIDAVIT OF CORRECTION/SURVEY" -> {..., CORRECTION, SURVEY}. This lets a
# single-token keyword like "ADMINISTRATOR" whole-word-match the possessive
# form without a fragile apostrophe-exact substring.
_WORD_RE = re.compile(r"[A-Z0-9]+")

# Company-name tokens for the pre-foreclosure person-vs-company heuristic
# (mirrors the acclaimweb template — a foreclosure where BOTH parties are
# companies is bank-vs-bank, not a homeowner lead). Module level so the nested
# helper references a constant (ruff B023).
_COMPANY_WORDS = {
    "INC", "LLC", "CORP", "CORPORATION", "BANK", "TRUST", "INSURANCE",
    "MORTGAGE", "SYSTEMS", "FINANCIAL", "NATIONAL", "SERVICES", "CLEARING",
    "REPUBLIC", "FARGO", "TITLE", "FEDERAL", "ASSOCIATION", "CREDIT", "UNION",
    "COMPANY", "CO", "CAPITAL", "PARTNERS", "GROUP", "SOLUTIONS", "AGENCY",
    "AUTHORITY", "DEPARTMENT", "LP", "REIT", "LENDING", "SERVICING", "MERS",
    "NA", "DBA", "FUND", "HOLDINGS", "PROPERTIES",
}

# Grantee token-set for recorder placeholders that are not real heirs — a
# one-sided filing (e.g. a Certificate of Death) lists "THE PUBLIC" as grantee.
# A grantee whose words are a subset of these is dropped (robust to trailing
# punctuation / whitespace vs an exact-string match).
_HEIR_NOISE_TOKENS = {"THE", "PUBLIC"}

# Split the requested window into <=30-day searches. The portal has no pager and
# caps a single search's result set server-side (~1000 rows); chunking keeps each
# search under the cap so no leads are silently dropped on busier counties.
_CHUNK_DAYS = 30

# Trailing fiduciary-role connector left dangling on a grantor when the estate
# name spilled to the other party line: "SMITH, JANE, ADMINISTRATOR OF" ->
# "SMITH, JANE". Anchored at end so it never cuts into the actual name.
_ROLE_TAIL_RE = re.compile(
    r",?\s+(?:AS\s+)?(?:SUCCESSOR\s+)?"
    r"(?:ADMINISTRATOR|ADMINISTRATRIX|EXECUTOR|EXECUTRIX|"
    r"PERSONAL\s+REPRESENTATIVE|TRUSTEE)\s+(?:OF|FOR)\s*$",
    re.IGNORECASE,
)

# Per-site doc type keywords. DO NOT merge with the equivalent maps in other
# templates — each is tuned to its underlying portal's doc-type vocabulary
# (verified against COLWA1's live Instrument dropdown + a full-year result
# sample). Single tokens are whole-word matched (regex tokenization above);
# multi-word entries are substring matched. Bare "AFFIDAVIT"/"DEATH"/"WILL"
# generics are intentionally excluded — they over-match unrelated filings
# (bare "DEATH" would wrongly catch "TRANSFER ON DEATH DEED", which a LIVING
# owner records for estate planning — not a deceased-owner lead).
_DOC_TYPE_MAP = {
    # Probate / estate signals that name the decedent or fiduciary as a party.
    # "PROBATE" (whole word) also catches "LACK OF PROBATE AFFIDAVIT".
    # "ADMINISTRATOR"/"ADMINISTRATRIX"/"EXECUTOR"/"EXECUTRIX"/"TESTAMENTARY"
    # whole-word match the possessive deed forms ("ADMINISTRATOR'S DEED" etc.).
    # Both word orders of the death certificate are listed (the portal emits
    # "CERTIFICATE OF DEATH"); the decedent is the grantor on those, verified
    # live (clean person names, no filing-agency-as-grantor).
    "probate": [
        "PROBATE", "TESTAMENTARY", "ADMINISTRATRIX", "EXECUTRIX",
        "ADMINISTRATOR", "EXECUTOR", "DECEDENT",
        "LETTERS OF ADMINISTRATION", "PERSONAL REPRESENTATIVE",
        "ESTATE OF", "DECREE OF DISTRIBUTION",
        "CERTIFICATE OF DEATH", "DEATH CERTIFICATE",
        "AFFIDAVIT OF SUCCESSOR",
    ],
    # Pre-foreclosure: the borrower (homeowner) is named on these. Excludes
    # SUBSTITUTION OF TRUSTEE / APPOINTMENT OF SUCCESSOR TRUSTEE and the
    # TRUSTEE'S DEED (completed-sale, post-foreclosure) admin docs.
    "pre_foreclosure": [
        "NOTICE OF TRUSTEE", "TRUSTEE'S SALE", "TRUSTEE SALE",
        "NOTICE OF DEFAULT", "NOTICE OF INTENT TO FORFEIT",
        "LIS PENDENS", "FORECLOSURE",
    ],
}


def _doc_type_matches(doc_type: str, keywords: list[str]) -> bool:
    """True if doc_type matches any keyword.

    Multi-word keywords use substring match ("NOTICE OF TRUSTEE" inside
    "AMENDED NOTICE OF TRUSTEE'S SALE"). Single-word keywords use whole-word
    match against the regex-tokenized doc string so "EXECUTOR" matches
    "EXECUTOR'S DEED" but not unrelated longer words.
    """
    doc_upper = doc_type.upper()
    doc_words = set(_WORD_RE.findall(doc_upper))
    for kw in keywords:
        if " " in kw:
            if kw in doc_upper:
                return True
        elif kw in doc_words:
            return True
    return False


class IDocMarketScraper(BridgeScraper):
    """Template scraper for all Tyler iDocMarket per-county search portals.

    Zero Claude AI cost — standardized server-rendered form + result-card
    extraction shared by every iDocMarket county.
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
    ):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (
            self.record_types[0] if self.record_types else None
        )

        domain = urlparse(base_url).hostname
        if domain:
            add_scrape_domain(domain)

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Scrape recorded documents for a date range from an iDocMarket portal.

        Args:
            date_from: Start date MM/DD/YYYY.
            date_to: End date MM/DD/YYYY.
        """
        active_rt = self.active_record_type
        # Fail CLOSED: we only emit records we can positively classify by doc
        # type. An unknown/missing record_type has no keyword list, so rather
        # than ingest every recorded document as a "lead", refuse and return
        # nothing (the job completes with 0, surfacing the misconfiguration).
        if not active_rt or active_rt not in _DOC_TYPE_MAP:
            _logger.error(
                "iDocMarket: no doc-type filter for record_type=%r (%s/%s) — refusing "
                "to ingest unclassified records",
                active_rt, self.county, self.state,
            )
            return []

        start = datetime.strptime(self._norm_date(date_from), "%m/%d/%Y")
        end = datetime.strptime(self._norm_date(date_to), "%m/%d/%Y")
        if start > end:
            raise ValueError(
                f"iDocMarket: date_from {date_from!r} is after date_to {date_to!r}"
            )
        _logger.info(
            "iDocMarket scraper — %s/%s %s — %s to %s (%d-day chunks)",
            self.county, self.state, active_rt,
            start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"), _CHUNK_DAYS,
        )

        # Chunk the window into <=_CHUNK_DAYS slices. The portal returns every
        # match for a date range on a single page (no pager) but caps the result
        # set server-side (~1000) — chunking keeps each search well under the cap
        # so no leads are silently dropped on busier counties. Dedup across chunks
        # guards the inclusive boundaries.
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=_CHUNK_DAYS - 1), end)
            cf, ct = chunk_start.strftime("%m/%d/%Y"), chunk_end.strftime("%m/%d/%Y")

            # Fail CLOSED on a chunk error: re-raise so the whole job fails and
            # the next scheduled run retries the FULL window. Returning the
            # already-extracted chunks would deliver a partial window as if it
            # were complete (missed leads look like "none recorded"). Log which
            # chunk failed first so the failure is actionable.
            try:
                await self.navigate(self.base_url)
                await self.page.wait_for_timeout(1_500)
                await self._fill_dates(cf, ct)
                await self._submit_search()
                chunk_records = await self._extract_results(active_rt)
            except Exception as exc:
                _logger.error(
                    "iDocMarket %s chunk %s-%s failed (%d chunk(s) extracted before "
                    "failure are discarded): %s",
                    self.county, cf, ct, len(all_records), str(exc)[:160],
                )
                raise

            new = self.dedupe_extend(chunk_records, seen_hashes, all_records)
            _logger.info("Chunk %s-%s: %d new records (total: %d)", cf, ct, new, len(all_records))

            chunk_start = chunk_end + timedelta(days=1)

        _logger.info(
            "iDocMarket complete — %s: %d %s records (enrichment runs after save)",
            self.county, len(all_records), active_rt,
        )
        return all_records

    @staticmethod
    def _clean_party(name: str) -> str:
        """Strip a dangling fiduciary-role connector off the end of a party name."""
        if not name:
            return name
        cleaned = _ROLE_TAIL_RE.sub("", name).strip().rstrip(",").strip()
        return cleaned or name

    @staticmethod
    def _is_person(name: str) -> bool:
        """Heuristic: True if a party name looks like a real person, not a company.

        Tokenizes on letter/digit runs (not bare whitespace) so a trailing comma
        can't hide a company token ("MERS," -> "MERS").
        """
        words = _WORD_RE.findall(name.upper())
        if any(w in _COMPANY_WORDS for w in words):
            return False
        # Real people have first + last names; one-word tokens are almost always
        # company abbreviations even when not in the keyword list.
        if len(words) < 2:
            return False
        return len(name) >= 5

    @staticmethod
    def _norm_date(d: str) -> str:
        """Return MM/DD/YYYY. Accepts MM/DD/YYYY or M/D/YYYY input."""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(d, fmt).strftime("%m/%d/%Y")
            except ValueError:
                continue
        return d

    async def _fill_dates(self, date_from: str, date_to: str) -> None:
        """Fill the Record Date range inputs.

        Raises on failure (form missing, or the value did not stick) rather than
        swallowing it — a search that runs with empty/wrong dates would return a
        wrong-window result set and mislabel it, which is worse than a failed job.
        """
        try:
            await self.page.wait_for_selector("#StartRecordDate", timeout=15_000)
        except Exception as exc:
            raise RuntimeError(
                f"iDocMarket search form not found at {self.base_url}: {str(exc)[:120]}"
            ) from exc
        await self.page.fill("#StartRecordDate", date_from)
        await self.page.fill("#EndRecordDate", date_to)
        # Verify BOTH values stuck. The portal reformats input (strips leading
        # zeros: "04/19/2026" -> "4/19/2026"), so compare parsed dates, not raw
        # strings — but still catch an empty / unchanged field on either end,
        # which would run a wrong-window search.
        for field, expected in (("#StartRecordDate", date_from), ("#EndRecordDate", date_to)):
            actual = (await self.page.input_value(field) or "").strip()
            if self._parse_date(actual) != self._parse_date(expected):
                raise RuntimeError(
                    f"iDocMarket date fill failed for {field} "
                    f"(got {actual!r}, expected {expected!r})"
                )
        _logger.info("Filled date range %s to %s", date_from, date_to)

    @staticmethod
    def _parse_date(d: str) -> datetime | None:
        """Parse a portal date (MM/DD/YYYY or M/D/YYYY), or None if unparseable."""
        try:
            return datetime.strptime(d.strip(), "%m/%d/%Y")
        except (ValueError, AttributeError):
            return None

    async def _submit_search(self) -> None:
        """Click the Search submit button and wait for results to render.

        Raises if no submit control exists (a structurally broken page) so the
        job fails loudly instead of extracting from an un-searched page.
        """
        submit = self.page.locator(
            "input[type=submit][value*='Search' i], "
            "button[type=submit]:has-text('Search'), "
            "input[type=submit]"
        ).first
        if await submit.count() == 0:
            raise RuntimeError("iDocMarket search submit button not found")
        try:
            async with self.page.expect_navigation(timeout=25_000):
                await submit.click()
        except Exception:
            # Some submits re-render without a full navigation event.
            await self.page.wait_for_timeout(3_000)
        # Wait for result cards; an empty window legitimately has none, so fall
        # through after a short wait (the "Showing: 0 of 0" counter confirms it).
        try:
            await self.page.wait_for_selector(".result-item", timeout=8_000)
        except Exception:
            await self.page.wait_for_timeout(1_500)
        await self.page.wait_for_timeout(1_000)

    async def _extract_results(self, active_rt: str) -> list[ScrapedRecord]:
        """Extract and doc-type-filter every result card on the current page.

        iDocMarket renders all matches for the searched window on a single page
        (no pager). We read the "Showing: A of B results" counter and warn if
        A < B (truncated by the server-side cap) so a shortened chunk is visible
        in the logs. ``active_rt`` is validated by the caller to be in
        ``_DOC_TYPE_MAP``, so a keyword list always exists (filtering is never
        silently disabled).
        """
        raw = await self.page.evaluate(
            """() => {
                const norm = el => (el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : '');
                const items = Array.from(document.querySelectorAll('.result-item'));
                const rows = items.map(it => {
                    const grantors = Array.from(
                        it.querySelectorAll('.full-parties .grantor-line .party-value')
                    ).map(s => norm(s)).filter(Boolean);
                    const grantees = Array.from(
                        it.querySelectorAll('.full-parties .grantee-line .party-value')
                    ).map(s => norm(s)).filter(Boolean);
                    // Fallback to the short-parties block if full-parties is absent.
                    let shortParties = [];
                    if (!grantors.length && !grantees.length) {
                        shortParties = Array.from(
                            it.querySelectorAll('.short-parties span')
                        ).map(s => norm(s)).filter(Boolean);
                    }
                    const dateSpan = it.querySelector('.col-md-1 span');
                    const legalSpans = it.querySelectorAll('.first-legals span');
                    return {
                        doc_type: norm(it.querySelector('.doc-title')),
                        docnum: norm(it.querySelector('[sort-desc=docnum]')),
                        date_recorded: norm(dateSpan),
                        grantors, grantees, shortParties,
                        legal: legalSpans.length > 1 ? norm(legalSpans[legalSpans.length - 1]) : '',
                    };
                });
                const counter = (document.body.innerText.match(/Showing:\\s*([\\d,]+)\\s+of\\s+([\\d,]+)/) || []);
                return {rows, shown: counter[1] || '', total: counter[2] || ''};
            }"""
        )

        rows = raw.get("rows", []) if raw else []
        shown, total = raw.get("shown", ""), raw.get("total", "")
        if shown and total and shown.replace(",", "") != total.replace(",", ""):
            _logger.warning(
                "iDocMarket results truncated: showing %s of %s — widen chunking if leads are missed",
                shown, total,
            )

        keywords = _DOC_TYPE_MAP[active_rt]

        records: list[ScrapedRecord] = []
        for item in rows:
            doc_type = (item.get("doc_type") or "").strip()

            # Filter by the requested record type. A row with no doc type can't
            # be classified — drop it so unrelated filings don't leak through.
            if not doc_type or not _doc_type_matches(doc_type, keywords):
                continue

            record = ScrapedRecord()
            record.doc_type = doc_type.upper() or None

            # Date — normalize M/D/YYYY -> MM/DD/YYYY.
            date_str = (item.get("date_recorded") or "").strip()
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
            if m:
                record.date_recorded = f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"

            # Parties. Stacked owners join as " / ".
            grantors = item.get("grantors") or []
            grantees = item.get("grantees") or []
            if not grantors and not grantees:
                # short-parties fallback: first span grantor, second grantee.
                sp = item.get("shortParties") or []
                if sp:
                    grantors = sp[:1]
                    grantees = sp[1:2]

            grantor = self._clean_party(normalize_party_text(" / ".join(grantors)))
            grantee = self._clean_party(normalize_party_text(" / ".join(grantees)))

            if active_rt == "pre_foreclosure":
                # A doc that keyword-matched but signals the foreclosure was
                # cancelled/cured (Discontinuance, Rescission) or is pure trustee
                # admin (Substitution of Trustee) is NOT active distress — drop
                # it before the person/company orientation below.
                if is_cancellation_or_admin(doc_type):
                    continue
                # Borrower orientation via the shared helper: keeps the PERSON
                # sub-name from a stacked "BORROWER / TRUSTEE COMPANY" cell (the
                # old whole-cell person check dropped that legit lead) and drops
                # bank-vs-trustee rows with no homeowner.
                oriented = orient_pre_foreclosure_party(grantor, grantee)
                if oriented is None:
                    continue
                record.party_name, record.heirs = oriented
            else:
                # Probate: the decedent / fiduciary is the grantor.
                if grantor:
                    record.party_name = grantor
                # Set heirs only if the grantee carries a real name — drop
                # recorder placeholders like "THE PUBLIC" (token-set check is
                # robust to trailing punctuation / extra whitespace).
                if grantee:
                    heir_tokens = set(_WORD_RE.findall(grantee.upper()))
                    if heir_tokens and not heir_tokens <= _HEIR_NOISE_TOKENS:
                        record.heirs = grantee

            # Doc number is a document id, NOT a legal description — keep it in
            # enrichment_data; legal_description holds the REAL legal only (None when
            # the index has none; never DOC# as a stand-in).
            docnum = (item.get("docnum") or "").strip().lstrip("#").strip()
            record.enrichment_data = {"instrument_number": docnum, "source": "idocmarket"}
            legal = (item.get("legal") or "").strip()
            if legal:
                record.legal_description = legal

            # Stable, unique per-row fingerprint (includes instrument_number via
            # enrichment_data in to_dict); never the bare doc#, which would collapse
            # multi-party rows under one document. Keeps tasks.py source_fingerprint
            # stable + distinct without depending on legal_description.
            record.raw_html_hash = self.make_hash(record.to_dict())

            if record.party_name or record.date_recorded:
                records.append(record)

        _logger.info(
            "iDocMarket extracted %d/%d cards matching %s",
            len(records), len(rows), active_rt,
        )
        return records
