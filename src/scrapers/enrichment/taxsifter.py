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

import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.api.middleware.security import validate_scraping_target
from src.config import settings
from src.utils.logger import setup_logger
from src.utils.pinned_http import pinned_session

_logger = setup_logger("scraper.enrichment.taxsifter")

# County -> TaxSifter origin. The ONLY hosts this module will contact.
TAXSIFTER_ORIGINS: dict[str, str] = {
    "douglas": "https://douglaswa-taxsifter.publicaccessnow.com",
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
_DIGITS = re.compile(r"\d{1,20}")
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
        link = card.select_one('div.nav a[href*="Assessor.aspx"]')
        if match_div is None or link is None:
            continue
        label = _norm(match_div.get_text(" "))
        if not label.endswith("(PARCEL OWNER)"):
            continue
        owner = label[: -len("(PARCEL OWNER)")].strip()
        if owner != q and not owner.startswith(q + " &"):
            continue
        params = dict(
            pair.split("=", 1) for pair in urlparse(link.get("href", "")).query.split("&")
            if "=" in pair
        )
        key, parcel = params.get("keyId", ""), params.get("parcelNumber", "")
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
    zip5 = re.match(r"\d{5}", span("lbZip"))
    if street and city and state:
        tail = f"{state} {zip5.group(0)}" if zip5 else state
        result["mailing"] = f"{street}, {city}, {tail}"

    table = soup.find(id="cphContent_ctl00_dvMarketValues")
    if table is not None:
        for row in table.find_all("tr"):
            cells = [" ".join(td.get_text(" ").split()) for td in row.find_all("td")]
            if len(cells) == 2 and cells[0].rstrip(":").upper() == "TOTAL" \
                    and re.fullmatch(r"\$[\d,]+", cells[1]):
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
        result = None
        if not self._accepted:
            self._accepted = self._accept_disclaimer()
            if not self._accepted:
                raise RuntimeError("TaxSifter disclaimer was not accepted")
        resp = self._get(_RESULTS_PATH, q=query)
        if self._html_ok(resp) and _AGREE_FIELD not in resp.text:
            hit = parse_taxsifter_results(resp.text, query)
            if hit is not None:
                key, parcel = hit
                page = self._get(_ASSESSOR_PATH, keyId=key, parcelNumber=parcel, typeID="1")
                if self._html_ok(page):
                    result = parse_taxsifter_assessor(page.text)
        self._cache[query] = result
        return result
