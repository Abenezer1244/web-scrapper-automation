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
    post_search,
    search_type_field,
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
    # A county whose leads carry a SHORTER id than its Geo ID: the land record is the
    # short id plus this suffix. Empty for every county without that evidence.
    land_suffix: str = ""
    short_id_len: int = 0

    @property
    def source_key(self) -> str:
        """Health / admission / provenance slug: one per county, so a block on one
        portal never cools the others."""
        return f"pacs_{self.county}"

    def land_id(self, query: str) -> str | None:
        """The Geo ID a short caller id names (zeros kept), or None when it is not one.

        Width is read off the caller's own spelling, never the zero-stripped key, so a
        shorter id is never padded into an eligible one (Codex).
        """
        if self.land_suffix and re.fullmatch(rf"[0-9]{{{self.short_id_len}}}", query or ""):
            return query + self.land_suffix
        return None


# origin + cid are per county on purpose (Codex): the vendor is shared, the
# deployment is not. Verified 2026-10-02 by loading each search form.
PACS_SITES: dict[str, PacsSite] = {
    "benton": PacsSite("benton", "https://propertysearch.co.benton.wa.us/propertyaccess/PropertySearch.aspx?cid=0", False),
    # Clallam leads carry the statewide layer's 12-digit id; the portal prints 16 digits.
    # The county's own parcel layer maps each 12-digit GEO_12 to ONE PROP_ID, and that is
    # the portal's id + "0000" row in 33 of 33 parcels checked live 2026-10-31. A base
    # can also hold other suffixes ("1000", "2001"), which stay ambiguous.
    "clallam": PacsSite("clallam", "https://websrv22.clallam.net/propertyaccess/PropertySearch.aspx?cid=0", False,
                        land_suffix="0000", short_id_len=12),
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
# Benton heads the column "Parcel # / Geo ID"; Clallam and Island head it "Geographic ID"
# (live 2026-10-31), which `geo\s*id` alone never matched.
_GEO_HEADER_RE = re.compile(r"geo(?:graphic)?\s*id|parcel", re.I)


def _http_get(session: requests.Session, url: str) -> requests.Response:
    return session.get(url, timeout=_TIMEOUT_S, allow_redirects=False)


def _http_post(session: requests.Session, url: str, data: dict) -> requests.Response:
    """The search postback, its one same-origin redirect followed (pacs.post_search)."""
    return post_search(session, url, data, _TIMEOUT_S)


def _form_tokens(html: str) -> dict[str, str] | None:
    """The ASP.NET hidden fields a postback must echo. None when this is not the form."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {i.get("name"): i.get("value", "") for i in soup.find_all("input") if i.get("name")}
    if "propertySearchOptions$geoid" not in fields or "__VIEWSTATE" not in fields:
        return None
    return {k: v for k, v in fields.items() if k.startswith("__")}


_PAGE_HEADING_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s+of\s+(\d+)")


def _grid_is_complete(soup: BeautifulSoup, data_rows: int) -> bool | None:
    """True when this page IS the whole result set, False when there is more, None
    when the page does not say. The heading reads "1 - 1 of 1"; a longer set prints
    page links in the paging cell (live 2026-10-31: "1 - 25 of 12772")."""
    heading = soup.find(id=re.compile(r"pageHeading"))
    m = _PAGE_HEADING_RE.search(heading.get_text(" ")) if heading else None
    if m is None:
        return None
    first, last, total = (int(g) for g in m.groups())
    paging = soup.find("td", class_="paging")
    if (paging is not None and paging.find("a")) or total > last:
        return False
    return first == 1 and last == total == data_rows


def parse_results(html: str, parcel_key: str, land_id: str | None = None
                  ) -> tuple[str, str | None, str | None]:
    """(outcome, prop_id, matched_key) for a search-results page, judged against
    ``parcel_key``; ``matched_key`` is the key the detail page must then name.

    The grid is matched on its Geo ID column, never on position: exactly one row
    whose cell normalises to our parcel is required. A page with rows for OTHER
    parcels only (the portal's prefix search) is ``parcel_not_found``; several
    rows for ours is ``ambiguous``.

    Two pages are NOT a settled "not found" (Codex): a row whose Geo ID only EXTENDS
    ours (Clallam prints 16 digits, `0630115504300000`, where another source has the
    12-digit `063011550430`) is ``ambiguous``: charged and capped, never re-fetched
    for free; and a row whose Geo ID cell cannot be read at all (``unparsed``).

    ``land_id`` (PacsSite.land_id, zeros kept) is the one extension a site has proven
    equal to the short id. Its row is ours only when it is the ONLY row extending our
    id and the page is the whole result set; otherwise ``ambiguous``, or ``unparsed``
    when the page does not say whether it is complete (Codex).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile(r"resultsTable"))
    if table is None:
        if "None found" in soup.get_text(" "):
            return PARCEL_NOT_FOUND, None, None
        return UNPARSED, None, None
    heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    geo_col = next((i for i, h in enumerate(heads) if _GEO_HEADER_RE.search(h)), None)
    if geo_col is None:
        return UNPARSED, None, None
    exact: list[str | None] = []
    land: list[str | None] = []
    extends_ours = unreadable = False
    data_rows = 0
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) <= geo_col:
            # A header row has no <td>; the paging footer ("Page: 1") is one spanning cell.
            # Anything else this short is a data row we could not read the identity of,
            # which must not let the page read as a settled "not found" (Codex).
            unreadable = unreadable or len(tds) > 1
            continue
        data_rows += 1
        text = tds[geo_col].get_text(" ", strip=True)
        cell = normalize_pacs_parcel(text)
        if cell is None:
            unreadable = True
            continue
        if cell == parcel_key:
            hits = exact
        elif land_id is not None and query_digits(text) == land_id:
            hits = land     # compared zeros kept: stripping must not make a match
        else:
            extends_ours = extends_ours or cell.startswith(parcel_key)
            continue
        link = tr.find("a", href=re.compile(r"prop_id=\d+", re.I))
        m = re.search(r"prop_id=(\d{1,12})", link["href"], re.I) if link else None
        hits.append(m.group(1) if m else None)
    if not exact and not land:
        if unreadable:
            return UNPARSED, None, None
        if extends_ours:
            return AMBIGUOUS, None, None
        return PARCEL_NOT_FOUND, None, None
    if len(exact) + len(land) > 1:
        return AMBIGUOUS, None, None
    if land:
        if unreadable:
            return UNPARSED, None, None
        if extends_ours:
            return AMBIGUOUS, None, None
        complete = _grid_is_complete(soup, data_rows)
        if complete is None:
            return UNPARSED, None, None
        if not complete:
            return AMBIGUOUS, None, None
    prop_id = (exact or land)[0]
    if not prop_id:
        return UNPARSED, None, None
    return FOUND, prop_id, parcel_key if exact else normalize_pacs_parcel(land_id)


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
                 query: str | None = None, mode: dict | None = None, land_id: str | None = None,
                 detail_parser=None) -> tuple[MailingAnswer, str | None]:
    """Search POST, then detail GET, for one parcel. (answer, block_reason).
    `mode` is the form's search-mode field (pacs.search_type_field); `land_id` is
    PacsSite.land_id for this parcel's callers, or None."""
    data = {**tokens, **(mode or {}), "propertySearchOptions$geoid": query or parcel_key,
            "propertySearchOptions$search": "Search"}
    resp, blocked = _request(lambda: _http_post(session, site.search_url, data), still_held)
    if blocked or resp is None:
        return MailingAnswer(REQUEST_FAILED), blocked
    outcome, prop_id, matched_key = parse_results(resp.text, parcel_key, land_id)
    if outcome != FOUND:
        return MailingAnswer(outcome), None
    time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
    if not still_held():
        return MailingAnswer(SOURCE_UNAVAILABLE), None
    url = pacs_detail_url(site.search_url, prop_id)
    resp, blocked = _request(lambda: _http_get(session, url), still_held)
    if blocked or resp is None:
        return MailingAnswer(REQUEST_FAILED), blocked
    # The detail page must name the row the grid matched, not either spelling (Codex).
    # The literal parse_detail(...) call is what scripts/repair_pacs_mailing_recovery.py
    # fingerprints as "#580 present": keep it spelled this way.
    if detail_parser is not None:
        return detail_parser(resp.text, matched_key), None
    return parse_detail(resp.text, matched_key), None


def resolve_mailing(county: str, parcel_ids: list[str], *, time_budget_s: float = CALL_BUDGET_S,
                    detail_parser=None) -> dict[str, MailingAnswer]:
    """Owner mailing addresses for one county's PACS parcels, keyed by CALLER id.

    ``detail_parser(html, parcel_key)`` replaces ``parse_detail`` for a caller that
    needs other cells of the same page (Benton tax balances); it gets the same
    lease, pacing, block handling and grid match, and returns an object with an
    ``outcome`` attribute.

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
    # key -> the land id every caller of that key names; None as soon as two callers
    # disagree (one 12-digit spelling, one not), so one caller never lends another
    # an equivalence its own spelling does not carry (Codex).
    land_ids: dict[str, str | None] = {}
    unsupported = 0
    for pid in parcel_ids:
        key = normalize_pacs_parcel(pid)
        if key is None and re.search(r"[A-Za-z]", str(pid or "")):
            # A real id in a format this module cannot key (Island prints
            # `R03213-049-3470`) was never asked about. It used to be recorded
            # PARCEL_NOT_FOUND - a settled answer - with no request at all. It stays
            # source_unavailable: deferred, not charged.
            unsupported += 1
        elif key is None:
            out[pid] = MailingAnswer(PARCEL_NOT_FOUND)   # empty or all zeros: no parcel
        else:
            by_key.setdefault(key, []).append(pid)
            q = query_digits(pid)
            if len(q) > len(queries.get(key, "")):
                queries[key] = q
            lid = site.land_id(q)
            land_ids[key] = lid if land_ids.get(key, lid) == lid else None
    if unsupported:
        _logger.info("%s: %d parcel id(s) in a format this lookup cannot search; deferred",
                     site.source_key, unsupported)
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
        mode = search_type_field(resp.text, "account-number")
        if mode is None:
            _logger.warning("%s: search form offers no account-number search; %d parcel(s) "
                            "deferred", site.source_key, len(by_key))
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
                                           query=queries[key], mode=mode, land_id=land_ids[key],
                                           detail_parser=detail_parser)
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
