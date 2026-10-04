"""Owner-name address lookup on a county's TerraScan TaxSifter assessor portal.

For counties whose assessor runs TaxSifter instead of Tyler PACS (Douglas WA). Same
contract as the PACS lookup (``pacs.parse_pacs_result_html``): an owner-name match is
WEAK evidence, so it fills ``property_address`` / ``mailing_address`` /
``assessed_value`` only when exactly one parcel matches, and NEVER returns a parcel
number (``parcel_id`` is the identity, dedup and billing key).

Security (Codex consult on this module, P1): every URL is built from a fixed per-county
origin and fixed paths. The disclaimer form's own ``action`` is ignored, redirects are
never followed (the one expected 302, after the disclaimer, is checked by hand), and
``keyId`` / ``parcelNumber`` from the results page must be short ASCII digit strings
before they go into the assessor URL as encoded query parameters. The origin passes
``validate_scraping_target(resolve=True)`` once and every request uses a
``pinned_session`` (resolved once, blocked addresses refused, connected to the checked
address).

Politeness: one session and one disclaimer acceptance per run, requests serialized at
least ``_SPACING_S`` apart, and repeated names answered from a per-run cache.

Logging carries counts and outcome categories only: no names, addresses or parcels.
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from src.api.middleware.security import validate_scraping_target
from src.config import settings
from src.utils.logger import setup_logger
from src.utils.pinned_http import pinned_session

_logger = setup_logger("scraper.enrichment.taxsifter")

# County -> TaxSifter origin. The ONLY hosts this module will contact.
# Okanogan (2026-10-02): the same PublicAccessNow deployment as Douglas (verified:
# disclaimer 302, div.result cards, Assessor.aspx?keyId=&parcelNumber=, the
# ParcelOwnerInfo1 lbAddress/lbCity/lbState/lbZip mailing block). It replaces the
# recorder template's surname search that took the FIRST 10-digit number on the
# page as the parcel: in production that assigned one county-owned parcel to two
# different leads, and that parcel then keyed dedup and billing.
TAXSIFTER_ORIGINS: dict[str, str] = {
    "douglas": "https://douglaswa-taxsifter.publicaccessnow.com",
    "okanogan": "https://okanoganwa-taxsifter.publicaccessnow.com",
}

# County -> TaxSifter base URL for the PARCEL-keyed mailing source (resolve_mailing
# below), as opposed to the owner-name fill. Whitman (verified 2026-10-03): TerraScan
# TaxSifter under /Taxsifter, the same markup as Douglas/Okanogan (ParcelOwnerInfo1
# lbAddress/lbCity/lbState/lbZip, lbParcelNumber echo, btnAgree, no RCW text on the
# disclaimer). Whitman's GIS page says its data may not be used "to generate commercial
# mailing lists" (RCW 42.56.070(9)); owner cleared all counties 2026-10-02 and asked for
# Whitman 2026-10-04, so it answers to COUNTY_GIS_RESTRICTED_MAILING_ENABLED.
TAXSIFTER_PARCEL_SITES: dict[str, str] = {
    "whitman": "https://terrascan.whitmancounty.net/Taxsifter",
}

_DISCLAIMER_PATH = "/Disclaimer.aspx"
_RESULTS_PATH = "/Search/Results.aspx"
_ASSESSOR_PATH = "/Assessor.aspx"
_AGREE_FIELD = "ctl00$cphContent$btnAgree"
# Where an accepted disclaimer 302s to (the live portal: /default.aspx, which then
# redirects to the results page). Anything else means the acceptance failed.
_AGREED_PATHS = frozenset({"/default.aspx", _RESULTS_PATH.lower()})
_SPACING_S = 1.0
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
_DIGITS = re.compile(r"[0-9]{1,20}")  # ASCII only, never Unicode digits
_PERSON_TOKEN = re.compile(r"[A-Z][A-Z'\-]*")
_ESTATE_PREFIX = re.compile(r"^(?:THE\s+)?(?:ESTATE|EST)\s+OF\s+")


def _norm(text: str | None) -> str:
    """Upper-case, NFKC, single spaces: the form both sides are compared in."""
    text = unicodedata.normalize("NFKC", text or "").upper()
    return " ".join(text.split())


def to_taxsifter_name(party_name: str | None) -> str | None:
    """A recorder party name as the TaxSifter owner query "LAST, FIRST M".

    Recorder indexes list people as "LAST FIRST MIDDLE"; TaxSifter needs the comma.
    Only the first of stacked names (" / ") is used, an "ESTATE OF" caption is
    dropped, and anything that is not 2+ plain alphabetic tokens (one comma at
    most) returns None rather than a guessed query.
    """
    name = _norm((party_name or "").split(" / ")[0])
    name = _ESTATE_PREFIX.sub("", name)
    if name.count(",") > 1:
        return None
    if "," in name:
        last, rest = (p.strip() for p in name.split(",", 1))
        tokens = [last, *rest.split()]
    else:
        tokens = name.split()
    if len(tokens) < 2 or not all(_PERSON_TOKEN.fullmatch(t) for t in tokens):
        return None
    return f"{tokens[0]}, {' '.join(tokens[1:])}"


def parse_taxsifter_results(html: str, query: str) -> tuple[str, str] | None:
    """(keyId, parcelNumber) of the ONE parcel this owner query matches, else None.

    A card counts when its role is "(Parcel Owner)" and its owner is the query
    exactly or the query as the first of co-owners ("QUERY & ..."). Zero matching
    parcels, or two or more distinct ones, is None: we cannot know which property
    is the filing party's.
    """
    q = _norm(query)
    soup = BeautifulSoup(html, "html.parser")
    matched: dict[str, str] = {}
    for card in soup.select("div.result"):
        match_div = card.select_one("div.details div.match")
        links = [
            urlparse(a.get("href", "")) for a in card.select("div.nav a[href]")
        ]
        links = [u for u in links if not u.scheme and not u.netloc
                 and u.path.lower() == _ASSESSOR_PATH.lower()]
        if match_div is None or len(links) != 1:
            continue
        label = _norm(match_div.get_text(" "))
        if not label.endswith("(PARCEL OWNER)"):
            continue
        owner = label[: -len("(PARCEL OWNER)")].strip()
        if owner != q and not owner.startswith(q + " &"):
            continue
        params = parse_qs(links[0].query, keep_blank_values=True)
        keys, parcels = params.get("keyId", []), params.get("parcelNumber", [])
        if len(keys) != 1 or len(parcels) != 1:
            continue
        key, parcel = keys[0], parcels[0]
        if not (_DIGITS.fullmatch(key) and _DIGITS.fullmatch(parcel)):
            continue
        matched[parcel] = key
    if len(matched) != 1:
        return None
    parcel, key = next(iter(matched.items()))
    return key, parcel


def _mailing_from(soup) -> str | None:
    """"STREET, CITY, ST ZIP5" from an Assessor.aspx owner block; None when incomplete."""
    def span(suffix: str) -> str:
        el = soup.find(id=f"cphContent_ParcelOwnerInfo1_{suffix}")
        return " ".join(el.get_text(" ").split()) if el else ""

    street = " ".join(p for p in (span("lbAddress"), span("lbAddress2")) if p)
    city, state = span("lbCity"), span("lbState")
    zip5 = re.match(r"[0-9]{5}", span("lbZip"))
    if not (street and city and state):
        return None
    tail = f"{state} {zip5.group(0)}" if zip5 else state
    return f"{street}, {city}, {tail}"


def parse_taxsifter_assessor(html: str) -> dict[str, str] | None:
    """{address, mailing?, value?} from an Assessor.aspx page; None without a situs."""
    soup = BeautifulSoup(html, "html.parser")

    def span(suffix: str) -> str:
        el = soup.find(id=f"cphContent_ParcelOwnerInfo1_{suffix}")
        return " ".join(el.get_text(" ").split()) if el else ""

    situs = span("lbSitus")
    if not situs:
        return None
    result = {"address": situs}

    mailing = _mailing_from(soup)
    if mailing:
        result["mailing"] = mailing

    table = soup.find(id="cphContent_ctl00_dvMarketValues")
    if table is not None:
        for row in table.find_all("tr"):
            cells = [" ".join(td.get_text(" ").split()) for td in row.find_all("td")]
            if len(cells) == 2 and cells[0].rstrip(":").upper() == "TOTAL" \
                    and re.fullmatch(r"\$[0-9,]+", cells[1]):
                result["value"] = cells[1]
                break
    return result


class TaxSifterClient:
    """One run's serialized, cached TaxSifter lookups for one county.

    Blocking (requests); the caller runs ``lookup`` off the event loop. Not
    thread-safe by design: one client, one thread, one request at a time.
    """

    def __init__(self, county: str, *, parcel_site: bool = False):
        # The two registries never leak into each other: a parcel-only portal (Whitman)
        # is not an owner-name source, and an owner-name portal is not a parcel source.
        origin = (TAXSIFTER_PARCEL_SITES if parcel_site else TAXSIFTER_ORIGINS).get(county.lower())
        if origin is None:
            raise ValueError("no TaxSifter origin for this county")
        validate_scraping_target(origin, require_allowlisted=False, resolve=True)
        self._origin = origin
        self._session = pinned_session()
        self._session.headers["User-Agent"] = _USER_AGENT
        self._accepted = False
        self._last_request = 0.0
        self._cache: dict[str, dict[str, str] | None] = {}

    def _get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    def _request(self, method: str, path: str, **kwargs):
        wait = self._last_request + _SPACING_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self._session.request(
                method, self._origin + path, timeout=settings.DEFAULT_TIMEOUT,
                allow_redirects=False, **kwargs,
            )
            self.last_status = resp.status_code  # lets a caller see a 403/429 refusal
            return resp
        finally:
            self._last_request = time.monotonic()

    def _html_ok(self, resp) -> bool:
        return resp.status_code == 200 and "html" in resp.headers.get("Content-Type", "").lower()

    def _accept_disclaimer(self) -> bool:
        page = self._get(_DISCLAIMER_PATH)
        if not self._html_ok(page):
            return False
        form = BeautifulSoup(page.text, "html.parser").find("form")
        if form is None:
            return False
        data = {
            i["name"]: i.get("value", "")
            for i in form.find_all("input", attrs={"type": "hidden"}) if i.get("name")
        }
        data[_AGREE_FIELD] = "I Agree"
        resp = self._request("POST", _DISCLAIMER_PATH, data=data)
        if resp.status_code != 302:
            return False
        target = urlparse(urljoin(self._origin + _DISCLAIMER_PATH, resp.headers.get("Location", "")))
        expected = urlparse(self._origin)
        base = expected.path.rstrip("/").lower()  # Whitman serves the portal under /Taxsifter
        return (target.scheme, target.netloc) == (expected.scheme, expected.netloc) \
            and target.path.lower() in {base + p for p in _AGREED_PATHS}

    def lookup(self, party_name: str | None) -> dict[str, str] | None:
        """{address, mailing?, value?} for a unique owner match; never a parcel."""
        query = to_taxsifter_name(party_name)
        if query is None:
            return None
        if query in self._cache:
            return self._cache[query]
        if not self._accepted:
            self._accepted = self._accept_disclaimer()
            if not self._accepted:
                raise RuntimeError("TaxSifter disclaimer was not accepted")
        resp = self._get(_RESULTS_PATH, q=query)
        if not self._html_ok(resp) or _AGREE_FIELD in resp.text:
            raise RuntimeError("TaxSifter results page was not served")
        hit = parse_taxsifter_results(resp.text, query)
        result = None
        if hit is not None:
            key, parcel = hit
            page = self._get(_ASSESSOR_PATH, keyId=key, parcelNumber=parcel, typeID="1")
            if not self._html_ok(page):
                raise RuntimeError("TaxSifter assessor page was not served")
            result = parse_taxsifter_assessor(page.text)
        # Only a definite answer (found, or no unique owner match) is cached; a
        # transport or page failure raised above, so that name can be retried.
        self._cache[query] = result
        return result


async def fill_addresses_by_owner(county: str, records: list) -> int:
    """Fill property/mailing address (and assessed value) on scraped records from a
    unique owner-name match on the county's TaxSifter. Returns how many were filled.

    Shared by every recorder template whose county runs TaxSifter (AcclaimWeb for
    Douglas, Tyler SelfService for Okanogan). Same contract as the PACS name
    lookup: address, mailing and value only, and only for a UNIQUE owner match;
    ``parcel_id`` is never set from an owner-name lookup (it is the identity,
    dedup and billing key). Lookups run serialized on one worker thread (one
    session, one disclaimer acceptance, polite spacing), off the event loop.
    """
    loop = asyncio.get_running_loop()
    try:
        client = await loop.run_in_executor(None, TaxSifterClient, county)
    except Exception as exc:
        _logger.warning("TaxSifter unavailable for %s: %s", county, type(exc).__name__)
        return 0

    found = failures = 0
    for record in records:
        try:
            result = await loop.run_in_executor(None, client.lookup, record.party_name)
            failures = 0
        except Exception as exc:
            # One failed lookup is skipped. A refused disclaimer, or three
            # failures in a row, ends the pass: the rest would fail the same way.
            failures += 1
            _logger.warning("TaxSifter lookup failed for %s: %s", county, type(exc).__name__)
            if "disclaimer" in str(exc) or failures >= 3:
                break
            continue
        if not result:
            continue
        # Fill-only on both addresses: a value the recorder document itself carried
        # outranks one inferred from an owner name (Codex P2). Every value written
        # here is stamped with where it came from, like every other mailing source.
        record.enrichment_data = record.enrichment_data or {}
        source = f"taxsifter_{county}"
        if not record.property_address and result.get("address"):
            record.property_address = result["address"]
            record.enrichment_data["property_source"] = source
            # TaxSifter's situs is street-only, but the site lists only its own WA
            # county's parcels, so the state is a fact, not a guess. Without it the
            # owner flags cannot be computed. Never overwrites a state already known.
            if not getattr(record, "property_state", None):
                record.property_state = "WA"
        if result.get("mailing") and not record.mailing_address:
            record.mailing_address = result["mailing"]
            record.enrichment_data["mailing_source"] = source
        if result.get("value"):
            record.enrichment_data["assessed_value"] = result["value"]
        found += 1
    _logger.info("TaxSifter lookup (%s): found addresses for %d/%d records",
                 county, found, len(records))
    return found


# ─── Parcel-keyed mailing (Whitman) ──────────────────────────────────────────
#
# The owner-name fill above never yields a parcel. This path is the other way round:
# the lead already HAS a parcel (EagleWeb reads it off the recorder document), and the
# county's own page is asked for that parcel's taxpayer mailing. Identity closes twice,
# like pacs_parcel: the search must return exactly one assessor link whose
# parcelNumber is ours, and that page must echo our parcel in lbParcelNumber. Same
# outcome vocabulary as pacs_parcel: only ``found`` carries an address; ``none``,
# ``parcel_not_found`` and ``parcel_mismatch`` are settled; ``ambiguous``, ``unparsed``,
# ``request_failed`` and ``source_unavailable`` stay retryable.

_RECORDS_FOUND_RE = re.compile(r"\b([0-9]{1,6})\s+records?\s+found\b", re.I)
_PARCEL_CALL_BUDGET_S = 120.0  # same share of the recovery tick as a PACS county
_PARCEL_LEASE_WAIT_S = 10.0
_PARCEL_UNPARSED_STREAK_LIMIT = 3
_BLOCK_STATUSES = (403, 429)


def parse_parcel_results(html: str, parcel_key: str) -> tuple[str, tuple[str, str] | None]:
    """(outcome, (keyId, parcelNumber)) for a parcel search, judged against ``parcel_key``
    (already normalised). "0 records found" is the only proof of parcel_not_found; a page
    without the record count is unparsed, never an answer about the parcel."""
    from src.scrapers.enrichment.pacs import normalize_pacs_parcel
    from src.scrapers.enrichment.pacs_parcel import PARCEL_NOT_FOUND, UNPARSED
    from src.scrapers.enrichment.snohomish_assessor_roll import AMBIGUOUS, FOUND

    soup = BeautifulSoup(html, "html.parser")
    count = _RECORDS_FOUND_RE.search(" ".join(soup.get_text(" ").split()))
    if count is None:
        return UNPARSED, None
    links: set[tuple[str, str]] = set()
    for a in soup.select("div.result a[href]"):
        u = urlparse(a.get("href", ""))
        if u.scheme or u.netloc or not u.path.lower().endswith("assessor.aspx"):
            continue
        params = parse_qs(u.query, keep_blank_values=True)
        keys, parcels = params.get("keyId", []), params.get("parcelNumber", [])
        if len(keys) != 1 or len(parcels) != 1:
            continue
        if not (_DIGITS.fullmatch(keys[0]) and _DIGITS.fullmatch(parcels[0])):
            continue
        links.add((keys[0], parcels[0]))
    # The count and the links must agree before anything is SETTLED (Codex P1): a page
    # that claims records but yields no readable link is layout drift, not an answer.
    if int(count.group(1)) == 0:
        # Any result card at all (readable link or not) contradicts "0 records".
        return (UNPARSED if links or soup.select("div.result") else PARCEL_NOT_FOUND), None
    if not links:
        return UNPARSED, None
    hits = {link for link in links if normalize_pacs_parcel(link[1]) == parcel_key}
    if not hits:
        return PARCEL_NOT_FOUND, None  # only OTHER parcels (the portal's prefix match)
    if len(hits) > 1:
        return AMBIGUOUS, None
    return FOUND, hits.pop()


def parse_parcel_assessor(html: str, parcel_key: str):
    """MailingAnswer for an Assessor.aspx page, which must name exactly our parcel."""
    from src.scrapers.enrichment.pacs import normalize_pacs_parcel
    from src.scrapers.enrichment.pacs_parcel import NONE, PARCEL_MISMATCH, UNPARSED
    from src.scrapers.enrichment.snohomish_assessor_roll import FOUND, MailingAnswer

    soup = BeautifulSoup(html, "html.parser")
    echo = soup.find(id="cphContent_ParcelOwnerInfo1_lbParcelNumber")
    if echo is None:
        return MailingAnswer(UNPARSED)
    if normalize_pacs_parcel(" ".join(echo.get_text(" ").split())) != parcel_key:
        return MailingAnswer(PARCEL_MISMATCH)  # never an address off another parcel's page
    if soup.find(id="cphContent_ParcelOwnerInfo1_lbAddress") is None:
        return MailingAnswer(UNPARSED)
    mailing = _mailing_from(soup)
    if mailing is None:
        return MailingAnswer(NONE)
    return MailingAnswer(FOUND, mailing_address=mailing, role="taxpayer")


def resolve_mailing(county: str, parcel_ids: list[str], *,
                    time_budget_s: float = _PARCEL_CALL_BUDGET_S) -> dict:
    """Taxpayer mailing for one county's parcels, keyed by CALLER id. Never raises.

    Every requested id gets an answer; parcels this call did not reach (license switch,
    lease, cooldown, budget, a block mid-pass) are ``source_unavailable`` so the caller
    defers them. One stream fleet-wide per county (SourceAdmission), every request
    spaced, a 403/429 stops the pass and cools the source.
    """
    from src.scrapers.enrichment.pacs import normalize_pacs_parcel
    from src.scrapers.enrichment.pacs_parcel import (
        PARCEL_NOT_FOUND,
        REQUEST_FAILED,
        UNPARSED,
        query_digits,
    )
    from src.scrapers.enrichment.snohomish_assessor_roll import (
        FOUND,
        SOURCE_UNAVAILABLE,
        MailingAnswer,
    )
    from src.scrapers.enrichment.source_admission import SourceAdmission
    from src.scrapers.enrichment.source_health import (
        SourceUnavailableError,
        check_source_or_raise,
        record_source_blocked,
    )

    county = (county or "").lower()
    out = {pid: MailingAnswer(SOURCE_UNAVAILABLE) for pid in parcel_ids}
    if county not in TAXSIFTER_PARCEL_SITES or not settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED:
        return out
    by_key: dict[str, list[str]] = {}
    queries: dict[str, str] = {}  # key -> the longest caller spelling (zeros kept)
    for pid in parcel_ids:
        key = normalize_pacs_parcel(pid)
        if key is None:
            out[pid] = MailingAnswer(PARCEL_NOT_FOUND)
            continue
        by_key.setdefault(key, []).append(pid)
        q = query_digits(pid)
        if len(q) > len(queries.get(key, "")):
            queries[key] = q
    if not by_key:
        return out

    source_key = f"taxsifter_{county}"
    counts: dict[str, int] = {}
    deadline = time.monotonic() + time_budget_s
    with SourceAdmission(source_key, max_wait_s=_PARCEL_LEASE_WAIT_S) as admission:
        if not admission.holds_lease:
            _logger.info("%s: lease not held; %d parcel(s) deferred", source_key, len(by_key))
            return out
        try:
            check_source_or_raise(source_key)
            client = TaxSifterClient(county, parcel_site=True)
            if not client._accept_disclaimer():
                if getattr(client, "last_status", None) in _BLOCK_STATUSES:
                    record_source_blocked(source_key, "TaxSifter disclaimer HTTP 403/429")
                _logger.warning("%s: disclaimer not accepted; %d parcel(s) deferred",
                                source_key, len(by_key))
                return out
        except SourceUnavailableError as exc:
            _logger.info("%s: %s; %d parcel(s) deferred", source_key, exc, len(by_key))
            return out
        except Exception as exc:  # noqa: BLE001 -- a source problem defers, never raises
            _logger.warning("%s unavailable: %s", source_key, type(exc).__name__)
            return out

        def _page(path: str, **params):
            """(response, blocked) for one spaced GET; None response on transport error."""
            try:
                resp = client._get(path, **params)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("%s request %s", source_key, type(exc).__name__)
                return None, False
            if resp.status_code in _BLOCK_STATUSES:
                return None, True
            if not client._html_ok(resp) or _AGREE_FIELD in resp.text:
                return None, False  # an error page or a bounce back to the disclaimer
            return resp, False

        streak = 0
        reserve = 2 * (_SPACING_S + settings.DEFAULT_TIMEOUT)
        for i, (key, callers) in enumerate(by_key.items()):
            if i and time.monotonic() + reserve > deadline:
                break
            if not admission.still_held():
                break
            resp, blocked = _page(_RESULTS_PATH, q=queries[key])
            answer = None
            if not blocked and resp is not None:
                outcome, link = parse_parcel_results(resp.text, key)
                if outcome != FOUND:
                    answer = MailingAnswer(outcome)
                elif time.monotonic() > deadline:
                    break  # the second request would overrun the budget: defer this parcel
                elif admission.still_held():
                    resp, blocked = _page(_ASSESSOR_PATH, keyId=link[0], parcelNumber=link[1],
                                          typeID="1")
                    if not blocked and resp is not None:
                        answer = parse_parcel_assessor(resp.text, key)
                else:
                    break
            if blocked:
                record_source_blocked(source_key, "TaxSifter HTTP 403/429")
                _logger.warning("%s blocked; pass stopped", source_key)
                break
            if answer is None:
                answer = MailingAnswer(REQUEST_FAILED)
            for pid in callers:
                out[pid] = answer
            counts[answer.outcome] = counts.get(answer.outcome, 0) + 1
            streak = streak + 1 if answer.outcome == UNPARSED else 0
            if streak >= _PARCEL_UNPARSED_STREAK_LIMIT:
                _logger.error("%s: %d unparsed pages in a row; layout drift, pass stopped",
                              source_key, streak)
                break
    # Counts only: no parcels, no addresses, no names in logs.
    _logger.info("%s: %d parcel(s) requested, outcomes %s", source_key, len(by_key), counts)
    return out
