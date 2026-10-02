"""Owner mailing addresses from Tyler/Harris PACS PropertyAccess portals, BY PARCEL.

WHY THIS EXISTS
---------------
Every lead in a county without its own mailing source is enriched through the WA
statewide parcel layer, which is situs-only and answers ``mailing_address=None`` by
design (county_gis.py). Measured 2026-10-02 on done jobs: Benton probate 0/7,
Chelan 0/2, Okanogan 0/13, Clark 0/1,634 (Clark has its own module, clark_pic).
Seven WA counties publish the owner mailing address per parcel on a PACS portal
with no login: Benton, Clallam, Jefferson, Grant, Whatcom, Island and Chelan. The
same ASP.NET form serves all of them (``propertySearchOptions$geoid``, verified on
each portal 2026-10-02; Island was offline for maintenance that day).

IDENTITY (Codex P1)
-------------------
``prop_id`` is the portal's own integer key, not our parcel, so a parcel is only
trusted when the chain closes on it twice: the results grid must hold EXACTLY ONE
row whose "Parcel # / Geo ID" cell is our parcel, and that row's detail page must
echo the same parcel in its identity cell. Any other page is ``parcel_mismatch``,
never an address. The owner-NAME search in pacs.py is not reused here: a name is
weak evidence and that path never carried a parcel at all.

Benton probate parcel ``131073011125003`` (admin job bc8d507c, BridgeLeads
mailing NULL) resolves through this chain to one record whose detail page shows
a "Mailing Address:" cell; the county publishes it, BridgeLeads did not read it.

OUTCOMES
--------
``found`` is the only outcome that carries an address. ``none`` (the owner block
prints no address), ``parcel_not_found`` and ``parcel_mismatch`` are settled
answers about the parcel. ``ambiguous`` (several grid rows, or co-owner cells that
disagree), ``unparsed``, ``request_failed`` and ``source_unavailable`` mean we do
not know and stay retryable; none of them is ever recorded as "no mailing".

PACING
------
One stream fleet-wide per county (SourceAdmission lease), every request paced,
a 403/429 stops the pass and opens the shared source_health cooldown for that
county. Two requests per parcel (search POST, detail GET) plus one form GET per
pass for the ASP.NET tokens.

LICENSING
---------
Portals whose pages carry the RCW 42.56.070 no-commercial-lists clause (Chelan,
Grant, Island, Whatcom) answer to the same kill switch as every other restricted
source, ``COUNTY_GIS_RESTRICTED_MAILING_ENABLED``. The product owner cleared them
on 2026-10-02. Only the mailing address is read; owner names are never stored or
logged.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from src.config import settings
from src.scrapers.enrichment.pacs import (
    _HEADERS,
    compose_pacs_mailing,
    normalize_pacs_parcel,
    pacs_detail_url,
    parse_pacs_detail_html,
)
from src.scrapers.enrichment.snohomish_assessor_roll import (
    AMBIGUOUS,
    FOUND,
    SOURCE_UNAVAILABLE,
    MailingAnswer,
)
from src.utils.logger import setup_logger
from src.utils.pinned_http import pinned_session

_logger = setup_logger("enrichment.pacs_parcel")

NONE = "none"
PARCEL_NOT_FOUND = "parcel_not_found"
PARCEL_MISMATCH = "parcel_mismatch"
UNPARSED = "unparsed"
REQUEST_FAILED = "request_failed"
SETTLED = frozenset({FOUND, NONE, PARCEL_NOT_FOUND, PARCEL_MISMATCH})


@dataclass(frozen=True)
class PacsSite:
    county: str
    search_url: str  # the PropertySearch.aspx page, with its cid
    license_restricted: bool

    @property
    def source_key(self) -> str:
        """Health / admission / provenance slug: one per county, so a block on one
        portal never cools the others."""
        return f"pacs_{self.county}"


# origin + cid are per county on purpose (Codex): the vendor is shared, the
# deployment is not. Verified 2026-10-02 by loading each search form.
PACS_SITES: dict[str, PacsSite] = {
    "benton": PacsSite("benton", "https://propertysearch.co.benton.wa.us/propertyaccess/PropertySearch.aspx?cid=0", False),
    "clallam": PacsSite("clallam", "https://websrv22.clallam.net/propertyaccess/PropertySearch.aspx?cid=0", False),
    "jefferson": PacsSite("jefferson", "https://trueweb.jeffcowa.us/propertyaccess/PropertySearch.aspx?cid=0", False),
    "grant": PacsSite("grant", "https://propertysearch.grantcountywa.gov/propertyaccess/PropertySearch.aspx?cid=10", True),
    "whatcom": PacsSite("whatcom", "https://property.whatcomcounty.us/propertyaccess/PropertySearch.aspx?cid=0", True),
    "island": PacsSite("island", "https://assessor.islandcountywa.gov/propertyaccess/PropertySearch.aspx?cid=0", True),
    "chelan": PacsSite("chelan", "https://pacs.co.chelan.wa.us/PropertyAccess/PropertySearch.aspx?cid=91", True),
}

_PACE_S = 3.0
_JITTER_S = 1.0
_TIMEOUT_S = 25
_RETRY_BACKOFF_S = 8.0
# One call's wall-clock budget; parcels past it defer to mailing recovery.
# ponytail: fixed budget per call, pass one in from the caller if a job needs more.
CALL_BUDGET_S = 120.0  # the recovery tick shares 480 s across every county
# The budget is checked before each parcel, so a call can overrun it by at most one
# parcel's worst case (two requests that each time out once): bounded, accepted.
_LEASE_WAIT_S = 10.0


def _reserve_s() -> float:
    """Seconds to keep in hand before starting another parcel: two paced requests
    that each run to their timeout once. Read at call time, not import time, so
    the pacing knobs stay honest when they change. The first parcel is always
    attempted; without that a budget shorter than one reservation did nothing."""
    return 2 * (_PACE_S + _JITTER_S + _TIMEOUT_S)
_UNPARSED_STREAK_LIMIT = 3
_BLOCK_STATUSES = (403, 429)
_GEO_HEADER_RE = re.compile(r"geo\s*id|parcel", re.I)


def _http_get(session: requests.Session, url: str) -> requests.Response:
    return session.get(url, timeout=_TIMEOUT_S, allow_redirects=False)


def _http_post(session: requests.Session, url: str, data: dict) -> requests.Response:
    """The search postback. PACS answers it with a 302 to SearchResults.aspx on the same
    portal; that ONE hop is followed by hand, and only when it stays on the portal's
    own origin (Codex P1, SSRF): a Location anywhere else is refused, never fetched."""
    from urllib.parse import urljoin

    from src.utils.safe_http import same_origin

    resp = session.post(url, data=data, timeout=_TIMEOUT_S, allow_redirects=False)
    if resp.status_code not in (302, 303):
        # 302/303 is what PACS sends (a GET of the results page). A 307/308 would ask
        # for the POST to be replayed; it is returned unfollowed (a 3xx is not 200,
        # so it counts as request_failed) rather than silently turned into a GET.
        return resp
    target = urljoin(url, resp.headers.get("Location", ""))
    if not same_origin(target, url):
        _logger.warning("pacs_parcel: refused an off-origin redirect from the search")
        resp.status_code = 400  # a client error: not retried, counted as request_failed
        return resp
    return session.get(target, timeout=_TIMEOUT_S, allow_redirects=False)


def _form_tokens(html: str) -> dict[str, str] | None:
    """The ASP.NET hidden fields a postback must echo. None when this is not the form."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {i.get("name"): i.get("value", "") for i in soup.find_all("input") if i.get("name")}
    if "propertySearchOptions$geoid" not in fields or "__VIEWSTATE" not in fields:
        return None
    return {k: v for k, v in fields.items() if k.startswith("__")}


def parse_results(html: str, parcel_key: str) -> tuple[str, str | None]:
    """(outcome, prop_id) for a search-results page, judged against ``parcel_key``.

    The grid is matched on its Geo ID column, never on position: exactly one row
    whose cell normalises to our parcel is required. A page with rows for OTHER
    parcels only (the portal's prefix search) is ``parcel_not_found``; several
    rows for ours is ``ambiguous``.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r"resultsTable"))
    if table is None:
        if "None found" in soup.get_text(" "):
            return PARCEL_NOT_FOUND, None
        return UNPARSED, None
    heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    geo_col = next((i for i, h in enumerate(heads) if _GEO_HEADER_RE.search(h)), None)
    if geo_col is None:
        return UNPARSED, None
    hits: list[str | None] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) <= geo_col:
            continue
        if normalize_pacs_parcel(tds[geo_col].get_text(" ", strip=True)) != parcel_key:
            continue
        link = tr.find("a", href=re.compile(r"prop_id=\d+", re.I))
        m = re.search(r"prop_id=(\d{1,12})", link["href"], re.I) if link else None
        hits.append(m.group(1) if m else None)
    if not hits:
        return PARCEL_NOT_FOUND, None
    if len(hits) > 1:
        return AMBIGUOUS, None
    return (FOUND, hits[0]) if hits[0] else (UNPARSED, None)


def parse_detail(html: str, parcel_key: str) -> MailingAnswer:
    """One Property.aspx page -> its answer about ``parcel_key`` (already normalised)."""
    page = parse_pacs_detail_html(html)
    if not page["geo_ids"]:
        return MailingAnswer(UNPARSED)
    if len(page["geo_ids"]) != 1 or normalize_pacs_parcel(page["geo_ids"][0]) != parcel_key:
        # Never take an address off a page that does not name exactly this parcel.
        return MailingAnswer(PARCEL_MISMATCH)
    blocks = page["mailing"]
    if not blocks:
        return MailingAnswer(UNPARSED)
    if all(not lines for lines in blocks):
        return MailingAnswer(NONE)
    if any(not lines for lines in blocks):
        # One owner prints an address and a co-owner prints none: the page does not
        # say whose address the parcel's mail goes to. Ambiguous, never found (Codex).
        return MailingAnswer(AMBIGUOUS)
    composed = {compose_pacs_mailing(lines) for lines in blocks}
    if None in composed:
        return MailingAnswer(UNPARSED)
    if len(composed) != 1:
        return MailingAnswer(AMBIGUOUS)  # co-owners with different addresses
    return MailingAnswer(FOUND, mailing_address=composed.pop(), role="owner")


def _request(do, still_held) -> tuple[requests.Response | None, str | None]:
    """One request with one retry on timeout/connection/5xx. (response, block_reason).

    A 403/429 is the county refusing us: the pass stops and the source cools down.
    A request that failed twice is REQUEST_FAILED at the caller: it cost the county
    a request, so it spends a recovery attempt rather than rotating for free.
    """
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(_RETRY_BACKOFF_S + random.uniform(0, _JITTER_S))  # noqa: S311
            if not still_held():
                return None, None
        try:
            resp = do()
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 1:
                continue
            _logger.warning("pacs_parcel request %s", type(exc).__name__)
            return None, None
        if resp.status_code in _BLOCK_STATUSES:
            return None, f"HTTP {resp.status_code}"
        if resp.status_code >= 500 and attempt == 1:
            continue
        if resp.status_code != 200:
            _logger.warning("pacs_parcel HTTP %d", resp.status_code)
            return None, None
        return resp, None
    return None, None


def query_digits(value: object) -> str:
    """What is SENT to the portal: the caller's parcel with separators removed and
    leading zeros KEPT. The county's search may be fixed-width (Clallam's Geo IDs
    start with 0); only the comparison key strips zeros (Codex P2)."""
    return re.sub(r"[\s.\-]", "", str(value or ""))


def _resolve_one(session, site: PacsSite, tokens: dict, parcel_key: str, still_held,
                 query: str | None = None) -> tuple[MailingAnswer, str | None]:
    """Search POST, then detail GET, for one parcel. (answer, block_reason)."""
    data = {**tokens, "propertySearchOptions$geoid": query or parcel_key,
            "propertySearchOptions$search": "Search"}
    resp, blocked = _request(lambda: _http_post(session, site.search_url, data), still_held)
    if blocked or resp is None:
        return MailingAnswer(REQUEST_FAILED), blocked
    outcome, prop_id = parse_results(resp.text, parcel_key)
    if outcome != FOUND:
        return MailingAnswer(outcome), None
    time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
    if not still_held():
        return MailingAnswer(SOURCE_UNAVAILABLE), None
    url = pacs_detail_url(site.search_url, prop_id)
    resp, blocked = _request(lambda: _http_get(session, url), still_held)
    if blocked or resp is None:
        return MailingAnswer(REQUEST_FAILED), blocked
    return parse_detail(resp.text, parcel_key), None


def resolve_mailing(county: str, parcel_ids: list[str], *, time_budget_s: float = CALL_BUDGET_S
                    ) -> dict[str, MailingAnswer]:
    """Owner mailing addresses for one county's PACS parcels, keyed by CALLER id.

    Every requested id gets an answer. Parcels this call did not reach (budget,
    lease, cooldown, a block mid-pass) are ``source_unavailable``, so the caller
    defers them. Never raises for a source problem.
    """
    from src.scrapers.enrichment.source_admission import SourceAdmission
    from src.scrapers.enrichment.source_health import (
        SourceUnavailableError,
        check_source_or_raise,
        record_source_blocked,
    )

    out = {pid: MailingAnswer(SOURCE_UNAVAILABLE) for pid in parcel_ids}
    site = PACS_SITES.get((county or "").lower())
    if site is None:
        return out
    if site.license_restricted and not settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED:
        return out
    by_key: dict[str, list[str]] = {}
    queries: dict[str, str] = {}  # key -> the longest caller spelling (zeros kept)
    for pid in parcel_ids:
        key = normalize_pacs_parcel(pid)
        if key is None:
            out[pid] = MailingAnswer(PARCEL_NOT_FOUND)
        else:
            by_key.setdefault(key, []).append(pid)
            q = query_digits(pid)
            if len(q) > len(queries.get(key, "")):
                queries[key] = q
    if not by_key:
        return out

    counts: dict[str, int] = {}
    deadline = time.monotonic() + time_budget_s
    with SourceAdmission(site.source_key, max_wait_s=_LEASE_WAIT_S) as admission:
        if not admission.holds_lease:
            _logger.info("%s: lease not held; %d parcel(s) deferred", site.source_key, len(by_key))
            return out
        try:
            check_source_or_raise(site.source_key)
        except SourceUnavailableError as exc:
            _logger.info("%s: %s; %d parcel(s) deferred", site.source_key, exc, len(by_key))
            return out
        session = pinned_session()
        session.headers.update(_HEADERS)
        time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
        resp, blocked = _request(lambda: _http_get(session, site.search_url), admission.still_held)
        if blocked:
            record_source_blocked(site.source_key, f"PACS search form {blocked}")
            return out
        tokens = _form_tokens(resp.text) if resp is not None else None
        if tokens is None:
            _logger.warning("%s: search form unreadable; %d parcel(s) deferred", site.source_key, len(by_key))
            return out
        streak = 0
        reserve = _reserve_s()
        for i, (key, callers) in enumerate(by_key.items()):
            if i and time.monotonic() + reserve > deadline:
                break
            time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
            if not admission.still_held():
                break
            answer, blocked = _resolve_one(session, site, tokens, key, admission.still_held,
                                           query=queries[key])
            if blocked:
                record_source_blocked(site.source_key, f"PACS {blocked}")
                _logger.warning("%s blocked (%s); pass stopped", site.source_key, blocked)
                break
            for pid in callers:
                out[pid] = answer
            counts[answer.outcome] = counts.get(answer.outcome, 0) + 1
            streak = streak + 1 if answer.outcome == UNPARSED else 0
            if streak >= _UNPARSED_STREAK_LIMIT:
                _logger.error("%s: %d unparsed pages in a row; layout drift, pass stopped",
                              site.source_key, streak)
                break
    # Counts only: no parcels, no addresses, no names in logs.
    _logger.info("%s: %d parcel(s) requested, outcomes %s", site.source_key, len(by_key), counts)
    return out
