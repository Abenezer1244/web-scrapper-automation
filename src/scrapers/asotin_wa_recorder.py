"""Asotin County (WA) recorder — Helion "Digital Research Room", behind a reCAPTCHA v2.

Portal: https://pras.asotincountywa.gov/DigitalResearchRoom/ (the platform whatcom_wa.py
reads, but with a reCAPTCHA on its disclaimer). The owner approved solving it through the
existing 2Captcha helper (2026-11-16); the disclaimer forbids no automated access.

Flow (read live 2026-11-16):
1. /Disclaimer: solve the reCAPTCHA (one token per session, never reused), submit
   "I Agree". The session then searches and pages with no further captcha.
2. /?mode=Advanced: ISO dates (``type=date``) and a server-side document subtype.
3. Results: "N Results Found Displaying results a - b", 50 cards a page, "Next 50".
   Exactly one result: the portal opens that document's details page instead.
   No results: "No matching results found", with the form echoing the search.

Record types, owner decision 2026-11-16:
- death_certificate (subtype 109). A certificate names the decedent (DIRECT) but no
  parcel. A parcel is the decedent's only when the county's owner field names them AND
  marks the estate (asotin_parcels.match_estate with no representatives and no claims
  address: the estate-marked tie is the only one left). Anything else is held.
- pre_foreclosure (subtype 39, the county's "Lis Penden"), delivered as LIS PENDENS: a
  pending suit against the parcel, not proof of a foreclosure. One lead per parcel the
  card prints; a card without one (or "SEE ATTACHED") is held, never given an invented
  parcel.

Divorce is not offered: no "Decree Dis" was recorded in 2025 or 2026.

Never a silent zero: an unreadable page, a search the page does not echo back, a skipped
or repeated page, too many pages, a parcel layer that does not answer, or a session the
portal will not unlock fails the run (ScraperExecutionError).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.doc_scope import from_keyword_map
from src.scrapers.doc_types import normalize_doc_type
from src.scrapers.enrichment import asotin_parcels as ap
from src.scrapers.preforeclosure import orient_pre_foreclosure_party
from src.scrapers.probate import orient_probate_party
from src.scrapers.reliability import ScraperExecutionError, detect_block
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.asotin_wa_recorder")

HOST = "pras.asotincountywa.gov"
ROOT = f"https://{HOST}/DigitalResearchRoom/"
DISCLAIMER_URL = ROOT + "Disclaimer?returnUrl=%2FDigitalResearchRoom%2F"
ADVANCED_URL = ROOT + "?mode=Advanced"
add_scrape_domain(HOST)

# record type -> (portal subtype value, the portal's label for it, the delivered doc_type)
SUBTYPES: dict[str, tuple[str, str, str]] = {
    "death_certificate": ("109", "DEATH CERTIFICATE", "DEATH CERTIFICATE"),
    "pre_foreclosure": ("39", "Lis Penden", "LIS PENDENS"),
}
_COUNTY, _STATE = "asotin", "WA"
# The portal answers a burst of searches with "Too Many Requests" (read live 2026-11-16):
# a few wide windows (lis pendens run ~2 a year, death certificates ~2 a month, far under
# a 50-card page), paced.
_CHUNK_DAYS = 180
_PAUSE_S = 5.0
_BLOCK_BACKOFF_S = 60.0   # one wait, then one replay of the window
_PAGE_SIZE = 50
_MAX_PAGES = 40
_MAX_UNLOCKS = 2

_HEADER_RE = re.compile(
    r"([\d,]+)\s+Results?\s+Found\s+Displaying\s+results?\s+([\d,]+)\s*-\s*([\d,]+)", re.I)
_CRITERIA_RE = re.compile(
    r"Search\s+Criteria:\s*Date:\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})\s*(.*?)\s*View\s+By",
    re.I | re.S)
_PARCEL_RE = re.compile(r"\d{9,20}")
_DETAIL_URL_RE = re.compile(r"/DigitalResearchRoom/Document/Details\?year=(\d{4})&document=(\d+)$")

Lookup = Callable[[str], list[dict]]


def _fail(reason: str, **context) -> ScraperExecutionError:
    """A failure that marks the job FAILED, never a zero (reliability.py)."""
    return ScraperExecutionError(_COUNTY, "helion", reason.removeprefix("asotin recorder: "), **context)


class SessionExpiredError(Exception):
    """The portal answered with its disclaimer: the session has to be unlocked again."""


class PortalBlockedError(Exception):
    """The portal refused the request (rate limit / bot wall): wait, then replay once."""


@dataclass(frozen=True)
class Card:
    number: str
    recorded: str              # 'MM/DD/YYYY hh:mm AM'
    doc_type: str
    direct: tuple[str, ...]
    indirect: tuple[str, ...]
    parcels: tuple[str, ...]   # every Tax# value exactly as printed


@dataclass(frozen=True)
class Page:
    state: str                 # results | detail | empty | disclaimer | blocked | unknown
    total: int = 0
    first: int = 0
    last: int = 0
    cards: tuple[Card, ...] = ()
    criteria: tuple[str, str, str] | None = None   # (from M/D/YYYY, to, subtype label)
    reason: str | None = None                       # why a blocked page refused


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def _labelled(el, label: str) -> str:
    """The text of ``el`` after its leading <strong>label</strong>."""
    strong = el.find("strong")
    whole = _text(el)
    lab = _text(strong) if strong is not None else ""
    return whole[len(lab):].strip() if lab and whole.startswith(lab) else whole.replace(label, "", 1).strip()


def _card(el) -> Card:
    link = el.select_one("a.recording-link")
    recorded = doc_type = ""
    for p in el.find_all("p"):
        lab = _text(p.find("strong"))
        if lab.startswith("Date Recorded"):
            recorded = _labelled(p, "Date Recorded:")
        elif lab.startswith("Doc Type"):
            doc_type = _labelled(p, "Doc Type:")
    direct: list[str] = []
    indirect: list[str] = []
    for li in el.select("li.result-subline"):
        lab = _text(li.find("strong")).upper()
        value = _labelled(li, lab)
        if lab.startswith("DIRECT"):
            direct.append(value)
        elif lab.startswith("INDIRECT"):
            indirect.append(value)
    parcels: list[str] = []
    for span in el.select("span.result-subline"):
        parts = span.find_all("span", recursive=False)
        if parts and _text(parts[0]).startswith("Tax#"):
            parcels.append(_text(parts[-1]))
    return Card(_text(link), recorded, doc_type, tuple(direct), tuple(indirect), tuple(parcels))


def _value_after(soup, label: str) -> str:
    """On a document's details page: the value column beside a label paragraph."""
    p = soup.find("p", string=lambda t: t is not None and t.strip() == label)
    col = p.find_parent("div") if p is not None else None
    value = col.find_next_sibling("div") if col is not None else None
    return _text(value)


def _detail_card(soup) -> Card:
    """A search with ONE result opens that document's details page instead of a list."""
    parties: dict[str, list[str]] = {"DIRECT": [], "INDIRECT": []}
    for h4 in soup.find_all("h4"):
        side = _text(h4).upper()
        ul = h4.find_next_sibling("ul")
        if side in parties and ul is not None:
            parties[side] = [_text(li) for li in ul.find_all("li") if _text(li)]
    parcels: list[str] = []
    for table in soup.find_all("table"):
        heads = [_text(th) for th in table.select("thead th")]
        if "Tax Parcel #" in heads:
            col = heads.index("Tax Parcel #")
            for tr in table.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) > col and _text(tds[col]):
                    parcels.append(_text(tds[col]))
    return Card(_text(soup.select_one(".label-document-number")), _value_after(soup, "Recording Date"),
                _value_after(soup, "Document Type"), tuple(parties["DIRECT"]),
                tuple(parties["INDIRECT"]), tuple(parcels))


def parse_page(html: str, url: str = "") -> Page:
    """What one portal page says. Pure: no network."""
    soup = BeautifulSoup(html, "html.parser")
    if ("/Disclaimer" in url or soup.select_one(".g-recaptcha") is not None
            or soup.select_one("form[action$='/Disclaimer']") is not None):
        return Page("disclaimer")
    reason = detect_block(html)
    if reason:
        return Page("blocked", reason=reason)
    h1 = soup.find("h1")
    if h1 is not None and _text(h1) == "Document Details":
        return Page("detail", 1, 1, 1, (_detail_card(soup),))
    header = soup.select_one("#search-results-header")
    if header is not None:
        text = _text(header)
        m = _HEADER_RE.search(text)
        c = _CRITERIA_RE.search(header.get_text(" ", strip=True))
        if not m:
            return Page("unknown")
        total, first, last = (int(g.replace(",", "")) for g in m.groups())
        cards = tuple(_card(e) for e in soup.select(".search-result"))
        criteria = (c.group(1), c.group(2), re.sub(r"\s+", " ", c.group(3)).strip()) if c else None
        return Page("results", total, first, last, cards, criteria)
    if "No matching results found" in _text(soup.body or soup):
        start = soup.select_one("#Criteria_Filter_RecordingDateStart")
        end = soup.select_one("#Criteria_Filter_RecordingDateEnd")
        sel = soup.select_one("#Filter_DocumentSubtype option[selected]")
        criteria = None
        if start is not None and end is not None and sel is not None:
            criteria = (start.get("value") or "", end.get("value") or "", _text(sel))
        return Page("empty", criteria=criteria)
    return Page("unknown")


def check_detail_binding(url: str, card: Card, *, via_search: bool) -> None:
    """A details page counts as this search's single result only when the search's own
    submission redirected to it and its URL names the instrument it shows."""
    m = _DETAIL_URL_RE.search(url)
    if not via_search or m is None or card.number != f"{m.group(1)}-{m.group(2)}":
        raise _fail("asotin recorder: a details page that is not this search's result")


async def with_backoff(run: Callable[[], Awaitable[list]], *,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                       wait: float = _BLOCK_BACKOFF_S) -> list:
    """Run ``run``; if the portal refuses it, wait once and replay it from scratch."""
    try:
        return await run()
    except PortalBlockedError:
        _logger.info("asotin recorder: portal refused a search, waiting %.0fs", wait)
    await sleep(wait)
    try:
        return await run()
    except PortalBlockedError as exc:
        raise _fail(f"asotin recorder: the portal refused the search ({exc})") from exc


def _mdy(d: datetime) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def check_page(page: Page, *, expected_first: int, start: datetime, end: datetime,
               subtype_label: str) -> None:
    """Raise unless ``page`` is the page this search should be on. Pure."""
    if page.state == "disclaimer":
        raise SessionExpiredError()
    if page.state == "empty":
        want = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), subtype_label)
        if page.criteria != want:
            raise _fail(f"asotin recorder: an empty answer for another search ({page.criteria})")
        if expected_first != 1:
            raise _fail("asotin recorder: results vanished while paging")
        return
    if page.state == "blocked":
        raise PortalBlockedError(page.reason)
    if page.state not in ("results", "detail"):
        raise _fail("asotin recorder: unreadable results page (layout changed?)")
    # A single result opens its details page, which echoes no search: its type is checked
    # below and its recording date against the window (check_progress).
    if page.state == "results" and (
            page.criteria is None or page.criteria[:2] != (_mdy(start), _mdy(end))
            or page.criteria[2].casefold() != subtype_label.casefold()):
        raise _fail(f"asotin recorder: results for another search ({page.criteria})")
    if page.first != expected_first or page.last < page.first or page.last > page.total:
        raise _fail(
            f"asotin recorder: page shows {page.first}-{page.last} of {page.total}, "
            f"expected to start at {expected_first}")
    if len(page.cards) != page.last - page.first + 1:
        raise _fail(
            f"asotin recorder: {len(page.cards)} cards for results {page.first}-{page.last}")
    for card in page.cards:
        if not (card.number and card.recorded and card.doc_type):
            raise _fail("asotin recorder: a card without number, date or type")
        if card.doc_type.casefold() != subtype_label.casefold():
            raise _fail(
                f"asotin recorder: a {card.doc_type!r} card in a {subtype_label!r} search")


def search_windows(start: datetime, end: datetime, days: int = _CHUNK_DAYS) -> list[tuple[datetime, datetime]]:
    """Inclusive, non-overlapping windows covering start..end (a same-day request is one
    window): the portal's date search includes both ends."""
    out = []
    cursor = start
    while cursor <= end:
        edge = min(cursor + timedelta(days=days - 1), end)
        out.append((cursor, edge))
        cursor = edge + timedelta(days=1)
    return out


def check_progress(page: Page, *, total: int | None, seen: set[str], start: datetime,
                   end: datetime) -> int:
    """Across the pages of one search: the total never changes, no instrument repeats,
    and every card was recorded inside the window. Returns the total. Pure but for
    ``seen``, which it fills."""
    if total is not None and page.total != total:
        raise _fail(f"asotin recorder: the result count moved from {total} to {page.total} while paging")
    for card in page.cards:
        if card.number in seen:
            raise _fail(f"asotin recorder: instrument {card.number} came back twice")
        seen.add(card.number)
        try:
            day = datetime.strptime(_recorded_mdy(card.recorded), "%m/%d/%Y")
        except ValueError as exc:
            raise _fail(f"asotin recorder: unreadable recording date {card.recorded!r}") from exc
        if not start <= day <= end:
            raise _fail(f"asotin recorder: instrument {card.number} recorded outside the window")
    return page.total


def _recorded_mdy(recorded: str) -> str:
    return recorded.split()[0]


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _street_zip(lead: ap.ParcelLead) -> tuple[str, str | None]:
    return (lead.property_address or "").strip(), (lead.property_zip or "").strip() or None


def death_certificate_records(card: Card, lookup: Lookup) -> tuple[list[ScrapedRecord], list[str]]:
    """(leads, held reasons) for one death certificate. ``lookup`` asks the county parcel
    layer for a surname and raises ap.LayerError when it cannot answer."""
    decedent, heirs = orient_probate_party(card.direct[0] if card.direct else None,
                                           "; ".join(card.indirect) or None, card.doc_type)
    if not decedent:
        return [], ["no_decedent"]
    natural = recorder_to_natural(decedent)
    name = ap.notice_name(natural) if natural else None
    if name is None:
        return [], ["decedent_name_unreadable"]
    try:
        rows = lookup(name.last)
    except ValueError:
        return [], ["surname_not_queryable"]
    # No representatives and no claims address: the estate-marked owner is the only tie.
    match = ap.match_estate(natural, [], None, rows)
    records = []
    for lead in match.leads:
        street, zipc = _street_zip(lead)
        if not (lead.parcel_id and lead.party_name and street and (lead.mailing_address or "").strip()):
            match.held.append("parcel_missing_required_fields")
            continue
        rec = ScrapedRecord(
            date_recorded=_recorded_mdy(card.recorded), party_name=lead.party_name, heirs=heirs,
            doc_type=SUBTYPES["death_certificate"][2], parcel_id=lead.parcel_id,
            property_address=street, mailing_address=lead.mailing_address.strip(),
            property_zip=zipc, property_state=_STATE,
            enrichment_data={"source": "asotin_helion", "instrument_number": card.number,
                             "parcel_ties": list(lead.ties), "matched_by_name": True},
            raw_html_hash=_hash("death_certificate", card.number, lead.parcel_id))
        records.append(rec)
    return records, ([] if records else (match.held or ["no_tied_parcel"]))


def lis_pendens_records(card: Card) -> tuple[list[ScrapedRecord], list[str]]:
    """One lead per parcel the card prints; none (or SEE ATTACHED) is held."""
    # Each indexed party is judged on its own: the helper splits stacked names only on
    # ' / ', so joining a bank and a person on one side would lose the person.
    direct = [p for p in (_person(n) for n in card.direct) if p]
    indirect = [p for p in (_person(n) for n in card.indirect) if p]
    if direct:
        party, other = " / ".join(direct), "; ".join(card.indirect) or None
    elif indirect:
        party, other = " / ".join(indirect), "; ".join(card.direct) or None
    else:
        return [], ["no_person_party"]      # bank against trustee: no homeowner lead
    parcels = [p for p in card.parcels if _PARCEL_RE.fullmatch(p)]
    if not parcels:
        return [], ["no_parcel_on_record"]
    records = [ScrapedRecord(
        date_recorded=_recorded_mdy(card.recorded), party_name=party,
        doc_type=SUBTYPES["pre_foreclosure"][2], parcel_id=parcel, property_state=_STATE,
        enrichment_data={"source": "asotin_helion", "instrument_number": card.number,
                         "all_parcels": list(card.parcels), "counterparty": other},
        raw_html_hash=_hash("pre_foreclosure", card.number, parcel)) for parcel in parcels]
    return records, []


def _person(name: str) -> str | None:
    """The person(s) in one indexed party, or None for a company, agency or trustee."""
    oriented = orient_pre_foreclosure_party(name, None)
    return oriented[0] if oriented else None


async def with_unlock(unlock: Callable[[], Awaitable[None]],
                      search: Callable[[], Awaitable[list]], *, unlocked: bool,
                      max_unlocks: int = _MAX_UNLOCKS) -> list:
    """Run ``search``, unlocking first when the session is locked and again (at most
    ``max_unlocks`` times in all) whenever it answers with the disclaimer."""
    unlocks = 0
    while True:
        if not unlocked:
            if unlocks >= max_unlocks:
                raise _fail("asotin recorder: the session kept expiring")
            unlocks += 1
            await unlock()
            unlocked = True
        try:
            return await search()
        except SessionExpiredError:
            unlocked = False
            _logger.info("asotin recorder: session expired, unlocking again")


_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def recorder_to_natural(name: str) -> str | None:
    """'HOLLOWAY, MARGARET J' -> 'MARGARET J HOLLOWAY' (a suffix stays last)."""
    if "," not in name:
        return None
    last, rest = (s.strip() for s in name.split(",", 1))
    toks = rest.replace(".", " ").split()
    suffix = [t for t in toks if t.upper() in _SUFFIXES]
    given = [t for t in toks if t.upper() not in _SUFFIXES]
    if not last or not given:
        return None
    return " ".join([*given, last, *suffix])


class AsotinWARecorderScraper(BridgeScraper):
    """Asotin death certificates and lis pendens from the Helion recorder portal."""

    # An empty answer is proven to be this search (check_page), so the canary may take
    # an empty week as a verified zero (scheduler_helpers/health.py).
    CANARY_ACCEPTS_VALIDATED_EMPTY = True

    @classmethod
    def collection_scope(cls, record_type: str):
        if record_type not in SUBTYPES:
            return None
        return from_keyword_map({record_type: [SUBTYPES[record_type][2]]}, record_type, exact=True)

    def __init__(self, record_type: str = "death_certificate", doc_types: list[str] | None = None,
                 lookup: Lookup | None = None):
        super().__init__()
        if record_type not in SUBTYPES:
            raise ValueError(f"Asotin's recorder offers {sorted(SUBTYPES)}, not {record_type!r}")
        if doc_types is not None:
            wanted = {normalize_doc_type(d) or d for d in doc_types}
            if record_type != "pre_foreclosure" or "lis_pendens" not in wanted:
                raise ValueError("Asotin's recorder collects lis pendens only; the selection excludes it")
        self.record_type = record_type
        self._subtype, self._label, _ = SUBTYPES[record_type]
        self._lookup = lookup or ap.query_surname
        self._unlocked = False

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        try:
            start = datetime.strptime(date_from, "%m/%d/%Y")
            end = datetime.strptime(date_to, "%m/%d/%Y")
        except (TypeError, ValueError) as exc:
            raise _fail(f"asotin recorder: unusable date window {date_from!r}..{date_to!r}") from exc
        if end < start:
            raise _fail(f"asotin recorder: inverted date window {date_from}..{date_to}")
        records: list[ScrapedRecord] = []
        held: dict[str, int] = {}
        cards = 0
        for i, (c_start, c_end) in enumerate(search_windows(start, end)):
            if i:
                await asyncio.sleep(_PAUSE_S)      # the portal throttles bursts
            self.report_stage("searching")   # unlocking reports 'connecting' itself
            window = await with_backoff(
                lambda a=c_start, b=c_end: self._search_with_unlock(a, b))
            for card in window:
                cards += 1
                if self.record_type == "death_certificate":
                    try:
                        recs, why = death_certificate_records(card, self._lookup)
                    except ap.LayerError as exc:
                        raise _fail(
                            f"asotin recorder: county parcel layer did not answer ({exc})") from exc
                else:
                    recs, why = lis_pendens_records(card)
                records.extend(recs)
                for reason in why:
                    held[reason] = held.get(reason, 0) + 1
        _logger.info("asotin recorder %s %s..%s: %d card(s), %d lead(s), held %s",
                     self.record_type, date_from, date_to, cards, len(records), held or "none")
        return records

    async def _unlock(self) -> None:
        """Solve the disclaimer's reCAPTCHA with a fresh token and agree."""
        from src.scrapers.enrichment.captcha import forget_token, solve_recaptcha

        self.report_stage("connecting")
        await self.navigate(DISCLAIMER_URL)
        # The sitekey is read from the page (public, and it may change), never kept here.
        sitekey = await self.page.get_attribute(".g-recaptcha[data-sitekey]", "data-sitekey")
        if not sitekey:
            raise _fail("asotin recorder: the disclaimer carries no reCAPTCHA sitekey")
        forget_token(DISCLAIMER_URL, sitekey)       # never reuse a submitted token
        token = await solve_recaptcha(DISCLAIMER_URL, sitekey)
        if not token:
            raise _fail("asotin recorder: the reCAPTCHA was not solved")
        try:
            await self.page.evaluate(
                """t => { let a = document.querySelector('#g-recaptcha-response');
                          if (!a) { a = document.createElement('textarea');
                                    a.id = 'g-recaptcha-response'; a.name = 'g-recaptcha-response';
                                    a.style.display = 'none';
                                    document.querySelector("form[action$='/Disclaimer']").appendChild(a); }
                          a.value = t; }""", token)
            async with self.page.expect_navigation(timeout=30_000):
                await self.page.locator("form[action$='/Disclaimer'] button[type=submit]").first.click()
        finally:
            forget_token(DISCLAIMER_URL, sitekey)
        if parse_page(await self.page.content(), self.page.url).state == "disclaimer":
            raise _fail("asotin recorder: the portal refused the solved reCAPTCHA")
        self._unlocked = True

    async def _search_with_unlock(self, start: datetime, end: datetime) -> list[Card]:
        cards = await with_unlock(self._unlock, lambda: self._search(start, end),
                                  unlocked=self._unlocked)
        self._unlocked = True
        return cards

    async def _search(self, start: datetime, end: datetime) -> list[Card]:
        await self.navigate(ADVANCED_URL)
        form = parse_page(await self.page.content(), self.page.url)
        if form.state == "disclaimer":
            raise SessionExpiredError()
        if form.state == "blocked":
            raise PortalBlockedError(form.reason)
        self.report_stage("searching")       # again: an unlock in between said 'connecting'
        await self.page.locator("#Filter_DocumentSubtype").select_option(self._subtype)
        await self.page.locator("#Criteria_Filter_RecordingDateStart").fill(start.strftime("%Y-%m-%d"))
        await self.page.locator("#Criteria_Filter_RecordingDateEnd").fill(end.strftime("%Y-%m-%d"))
        async with self.page.expect_navigation(timeout=60_000) as nav:
            await self.page.locator("button:has-text('Search')").first.click()
        response = await nav.value
        origin = response.request.redirected_from if response is not None else None
        # The search posts to the portal root; one result is a redirect from that post.
        via_search = origin is not None and origin.url.rstrip("/") == ROOT.rstrip("/")
        self.report_stage("scraping")
        cards: list[Card] = []
        expected = 1
        total: int | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page = parse_page(await self.page.content(), self.page.url)
            check_page(page, expected_first=expected, start=start, end=end, subtype_label=self._label)
            if page.state == "empty":
                return []
            if page.state == "detail":
                check_detail_binding(self.page.url, page.cards[0], via_search=via_search and expected == 1)
            total = check_progress(page, total=total, seen=seen, start=start, end=end)
            cards.extend(page.cards)
            if page.last == page.total:
                if len(cards) != page.total:
                    raise _fail(
                        f"asotin recorder: read {len(cards)} of {page.total} results")
                return cards
            expected = page.last + 1
            nxt = self.page.locator(f"button:has-text('Next {_PAGE_SIZE}'):not([disabled])").first
            if not await nxt.count():
                raise _fail(
                    f"asotin recorder: {page.total - page.last} results left but no Next")
            async with self.page.expect_navigation(timeout=60_000):
                await nxt.click()
        raise _fail(f"asotin recorder: more than {_MAX_PAGES} pages")
