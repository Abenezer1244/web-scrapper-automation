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
- `#StartRecordDate` / `#EndRecordDate` <duet-date-picker> components: typed into
  their inner `#start-record-date` / `#end-record-date` inputs (MM/DD/YYYY); they
  submit hidden ISO `StartRecordDate` / `EndRecordDate` fields
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
from src.scrapers.probate import clean_counterparty, strip_estate_caption, strip_filing_agency
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

# Split the requested window into <=30-day searches. The portal has no pager and
# caps a single search's result set server-side (~1000 rows); chunking keeps each
# search under the cap so no leads are silently dropped on busier counties.
_CHUNK_DAYS = 30
# Pause between searches. ~12 unpaced searches in an hour got a 403 block (2026-11-14,
# lifted ~90 min later); a backfill of many chunks must not burst like that.
_SEARCH_PAUSE_MS = 20_000

# Trailing fiduciary-role connector left dangling on a grantor when the estate
# name spilled to the other party line: "SMITH, JANE, ADMINISTRATOR OF" ->
# "SMITH, JANE". Also a trailing status word: "POWERS, ZELLA, DECEASED" -> "POWERS,
# ZELLA". Anchored at end so it never cuts into the actual name.
_ROLE_TAIL_RE = re.compile(
    r",?\s+(?:(?:AS\s+)?(?:SUCCESSOR\s+)?"
    r"(?:ADMINISTRATOR|ADMINISTRATRIX|EXECUTOR|EXECUTRIX|"
    r"PERSONAL\s+REPRESENTATIVE|TRUSTEE)\s+(?:OF|FOR)|DECEASED)\s*$",
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
    # Probate = the Lack of Probate Affidavit only, on its whole label (owner decision
    # 2026-11-14). Its grantor is the decedent (verified: BLATT grantor -> grantee now
    # holds that legal on PACS). Fiduciary deeds and letters name the representative,
    # and a deed means the estate already sold, so neither is a probate lead here.
    "probate": ["LACK OF PROBATE AFFIDAVIT"],
    # The recorded certificate (the portal's label is "CERTIFICATE OF DEATH"); the
    # decedent is the grantor. Kept only on an exact label (_WHOLE_LABELS).
    "death_certificate": ["CERTIFICATE OF DEATH", "DEATH CERTIFICATE"],
    # Pre-foreclosure: the borrower (homeowner) is named on these. Excludes
    # SUBSTITUTION OF TRUSTEE / APPOINTMENT OF SUCCESSOR TRUSTEE and the
    # TRUSTEE'S DEED (completed-sale, post-foreclosure) admin docs.
    "pre_foreclosure": [
        "NOTICE OF TRUSTEE", "TRUSTEE'S SALE", "TRUSTEE SALE",
        "NOTICE OF DEFAULT", "NOTICE OF INTENT TO FORFEIT",
        "LIS PENDENS", "FORECLOSURE",
    ],
}


# Record types kept only on an exact whole label ("AFFIDAVIT OF DEATH", a TOD deed or
# an executor's deed is not one). The decedent is the grantor on every one of them.
_WHOLE_LABELS = {
    "death_certificate": frozenset({"CERTIFICATE OF DEATH", "DEATH CERTIFICATE"}),
    "probate": frozenset({"LACK OF PROBATE AFFIDAVIT"}),
}

# "Showing: A of B results" — the portal's own count for the searched window.
_COUNTER_RE = re.compile(r"Showing:\s*([\d,]+)\s+of\s+([\d,]+)")


def check_counter(text: str, cards: int) -> None:
    """Raise unless the page proves it listed the whole window.

    Unknown is never zero: a missing counter, shown != total (the server cap), or a
    card count that disagrees with the counter (selector drift) fails the chunk.
    "0 of 0" with no cards is the only zero.
    """
    m = _COUNTER_RE.search(text or "")
    if not m:
        raise RuntimeError("iDocMarket results counter not found: result count unknown")
    shown, total = (int(g.replace(",", "")) for g in m.groups())
    if not shown == total == cards:
        raise RuntimeError(
            f"iDocMarket results incomplete: showing {shown} of {total}, {cards} cards read"
        )


def keep_doc_type(doc_type: str, record_type: str, keywords: list[str]) -> bool:
    """Whether a row's document type belongs to ``record_type``."""
    if not doc_type or not _doc_type_matches(doc_type, keywords):
        return False
    labels = _WHOLE_LABELS.get(record_type)
    return labels is None or " ".join(doc_type.upper().split()) in labels


def decedent_party(grantor: str, grantee: str) -> tuple[str, str | None]:
    """``(party_name, heirs)`` for a death certificate / lack-of-probate affidavit.

    The grantor is the decedent; the grantee is never promoted in its place (a named
    grantee beside a placeholder grantor is an heir, not a death). A grantor that is
    empty or only a filing agency is not a readable decedent: the chunk fails.
    """
    party = strip_estate_caption(strip_filing_agency(grantor or ""))
    if not party:
        raise RuntimeError("iDocMarket decedent card without a readable grantor")
    return party, clean_counterparty(grantee or "")


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

    Standardized server-rendered form + result-card
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
        doc_types: list[str] | None = None,
    ):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self.active_record_type = record_type or (
            self.record_types[0] if self.record_types else None
        )
        # Phase B: narrow the client-side keyword filter to an explicit pre-foreclosure
        # selection (subset of _DOC_TYPE_MAP — registry tokens are an exact partition).
        # None = legacy/full; unmappable/empty raises (fail-closed).
        self._doc_type_keyword_override: list[str] | None = None
        if doc_types is not None and self.active_record_type == "pre_foreclosure":
            from src.scrapers.doc_types import canonical_tokens_or_raise
            self._doc_type_keyword_override = canonical_tokens_or_raise(
                county, state, list(dict.fromkeys(doc_types))
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
        # Fail CLOSED: we only emit records we can positively classify by doc type.
        # An unknown/missing record_type is a misconfiguration, never a zero.
        if not active_rt or active_rt not in _DOC_TYPE_MAP:
            raise ValueError(
                f"iDocMarket: no doc-type filter for record_type={active_rt!r} "
                f"({self.county}/{self.state})"
            )

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
                if chunk_start > start:
                    await self.page.wait_for_timeout(_SEARCH_PAUSE_MS)
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
        # The date fields are <duet-date-picker> components (since 2026-11): type into
        # the visible inner input; the component writes the ISO date to the hidden
        # StartRecordDate / EndRecordDate fields the form submits.
        try:
            await self.page.wait_for_selector("#start-record-date", timeout=15_000)
        except Exception as exc:
            raise RuntimeError(
                f"iDocMarket search form not found at {self.base_url}: {str(exc)[:120]}"
            ) from exc
        for visible, value in (("#start-record-date", date_from), ("#end-record-date", date_to)):
            await self.page.fill(visible, value)
            await self.page.press(visible, "Tab")  # the picker parses on blur
        # Verify the SUBMITTED values on both ends: an empty or unparsed field would
        # run a wrong-window search.
        for field, expected in (("StartRecordDate", date_from), ("EndRecordDate", date_to)):
            actual = (await self.page.eval_on_selector(
                f"input[name={field}]", "e => e.value") or "").strip()
            if self._parse_iso(actual) != self._parse_date(expected):
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

    @staticmethod
    def _parse_iso(d: str) -> datetime | None:
        """Parse the picker's submitted YYYY-MM-DD value, or None."""
        try:
            return datetime.strptime(d.strip(), "%Y-%m-%d")
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
        # Search is a full form POST (verified live 2026-11-14). A click that does not
        # navigate, or a non-200 answer (the portal's 403 block), fails the chunk: the
        # page left behind is not the result of this search.
        async with self.page.expect_navigation(timeout=60_000) as nav:
            await submit.click()
        response = await nav.value
        if response is None or response.status != 200:
            status = response.status if response is not None else "none"
            raise RuntimeError(f"iDocMarket search answered HTTP {status}")
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
        (no pager). ``check_counter`` fails the chunk unless the "Showing: A of B
        results" counter proves every row was read. ``active_rt`` is validated by the caller to be in
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
                    const dateSpan = it.querySelector('[aria-label^="Record Date"]');
                    const legalSpans = it.querySelectorAll('.first-legals span');
                    return {
                        doc_type: norm(it.querySelector('.doc-title')),
                        docnum: norm(it.querySelector('[sort-desc=docnum]')),
                        date_recorded: norm(dateSpan),
                        grantors, grantees, shortParties,
                        legal: legalSpans.length > 1 ? norm(legalSpans[legalSpans.length - 1]) : '',
                    };
                });
                return {rows, text: document.body.innerText};
            }"""
        )

        rows = raw.get("rows", []) if raw else []
        check_counter((raw or {}).get("text", ""), len(rows))

        keywords = self._doc_type_keyword_override or _DOC_TYPE_MAP[active_rt]

        records: list[ScrapedRecord] = []
        for item in rows:
            doc_type = (item.get("doc_type") or "").strip()

            # Every card the counter counted must be classifiable: a missing title is
            # selector drift, and skipping it would turn a counted card into a zero.
            if not doc_type:
                raise RuntimeError("iDocMarket card without a document type")
            if not keep_doc_type(doc_type, active_rt, keywords):
                continue

            record = ScrapedRecord()
            record.doc_type = doc_type.upper() or None

            # Date — normalize M/D/YYYY -> MM/DD/YYYY. A kept row without a record
            # date or document number is schema drift: fail, never deliver it undated.
            date_str = (item.get("date_recorded") or "").strip()
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
            docnum = (item.get("docnum") or "").strip().lstrip("#").strip()
            if not m or not docnum:
                raise RuntimeError(
                    f"iDocMarket {doc_type} card without record date or document number"
                )
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
            if not grantors and not grantees:
                raise RuntimeError(f"iDocMarket {doc_type} card without party names")

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
                # Probate / death certificate: the decedent is the grantor, never
                # swapped for the grantee ("THE PUBLIC" there is dropped).
                record.party_name, record.heirs = decedent_party(grantor, grantee)

            # Doc number is a document id, NOT a legal description — keep it in
            # enrichment_data; legal_description holds the REAL legal only (None when
            # the index has none; never DOC# as a stand-in).
            record.enrichment_data = {"instrument_number": docnum, "source": "idocmarket"}
            legal = (item.get("legal") or "").strip()
            if legal:
                record.legal_description = legal

            # Stable, unique per-row fingerprint (includes instrument_number via
            # enrichment_data in to_dict); never the bare doc#, which would collapse
            # multi-party rows under one document. Keeps tasks.py source_fingerprint
            # stable + distinct without depending on legal_description.
            record.raw_html_hash = self.make_hash(record.to_dict())

            records.append(record)

        _logger.info(
            "iDocMarket extracted %d/%d cards matching %s",
            len(records), len(rows), active_rt,
        )
        return records
