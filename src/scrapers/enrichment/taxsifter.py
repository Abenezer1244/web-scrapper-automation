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

    street = " ".join(p for p in (span("lbAddress"), span("lbAddress2")) if p)
    city, state = span("lbCity"), span("lbState")
    zip5 = re.match(r"[0-9]{5}", span("lbZip"))
    if street and city and state:
        tail = f"{state} {zip5.group(0)}" if zip5 else state
        result["mailing"] = f"{street}, {city}, {tail}"

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

    def __init__(self, county: str):
        origin = TAXSIFTER_ORIGINS.get(county.lower())
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
            return self._session.request(
                method, self._origin + path, timeout=settings.DEFAULT_TIMEOUT,
                allow_redirects=False, **kwargs,
            )
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
        return (target.scheme, target.netloc) == (expected.scheme, expected.netloc) \
            and target.path.lower() in _AGREED_PATHS

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
