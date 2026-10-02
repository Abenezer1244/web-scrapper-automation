"""Clark County (WA) owner mailing addresses from the Property Information Center.

WHY THIS EXISTS
---------------
Every Clark lead BridgeLeads ever stored had a NULL mailing address (probate 0/792,
pre_foreclosure 0/25, measured 2026-10-02 on admin job 62404bd0: 1,335 rows, 1,335
parcel ids, 1,292 property addresses, 0 mailing). Nothing was lost downstream: Clark
had no mailing source at all. It is not in county_gis._KNOWN_GIS_ENDPOINTS, so its
parcels fell through to the WA statewide layer, which is situs-only and returns
``mailing_address=None`` by design.

Clark's ArcGIS services do not fill the gap: ``ClarkView_Public/TaxlotsPublic`` has
situs columns but no owner or mailing columns (only ``MainOwnerID``), and the ``PIC``
and ``LandRecords`` folders answer "Token Required". The county's public Property
Information Center page does carry it, with no login:

    GET https://gis.clark.wa.gov/gishome/property/?pid=<parcel>&account=<parcel>

    Property Identification Number: <span class="picBasicInfo2">NNNNNNNNN</span>
    Owner Mailing Address<br /><span class="picBasicInfo2">
        <street><br /><CITY> <ST> , <ZIP><br/> US</span>

An unknown parcel answers HTTP 200 with "No Records Found.".

PACING (measured 2026-10-02)
----------------------------
Requests 1.5 s apart drew HTTP 429 on the 11th request; 20 requests ~7 s apart all
returned 200. So one stream fleet-wide (SourceAdmission lease), at least _PACE_S
apart plus jitter, and any 429/403 stops the pass and opens the shared source_health
cooldown (1 h first rung, escalating), which every process honours. The cooldown is
longer than any Retry-After this server has sent (it sends none), so it is honoured
by construction.

LICENSING
---------
The page footer cites RCW 42.56 (no commercial use of lists of individuals). The
product owner gave explicit legal clearance for Clark on 2026-10-02, extending the
2026-09-13 Snohomish/Cowlitz clearance, and this source answers to the same kill
switch (COUNTY_GIS_RESTRICTED_MAILING_ENABLED). Only the mailing address is read:
owner names are never stored or logged.

OUTCOMES
--------
``found`` is the only outcome that carries an address. ``none`` (the parcel exists
and the county publishes no mailing address), ``parcel_not_found`` and
``parcel_mismatch`` (the page named a different parcel) are settled answers.
``unparsed`` and ``request_failed`` (a request went out and did not produce a
readable page) stay retryable but spend a recovery attempt. ``source_unavailable``
(never asked: no lease, cooldown, budget, or blocked) stays retryable at no cost.
None of them is ever recorded as "this parcel has no mailing address".
"""
from __future__ import annotations

import random
import re
import time

import requests
from bs4 import BeautifulSoup

from src.scrapers.enrichment.snohomish_assessor_roll import (
    FOUND,
    SOURCE_UNAVAILABLE,
    MailingAnswer,
)
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("enrichment.clark_pic")

SOURCE = "clark_pic"
_URL = "https://gis.clark.wa.gov/gishome/property/"

NONE = "none"
PARCEL_NOT_FOUND = "parcel_not_found"
PARCEL_MISMATCH = "parcel_mismatch"
UNPARSED = "unparsed"
REQUEST_FAILED = "request_failed"

_PACE_S = 6.0
_JITTER_S = 2.0
_TIMEOUT_S = 20
_RETRY_BACKOFF_S = 8.0
# The longest one parcel can take: pace, two timed-out requests, and the backoff
# between them. A pass only starts a parcel that fits whole inside its budget.
_WORST_CASE_FETCH_S = _PACE_S + 2 * _JITTER_S + 2 * _TIMEOUT_S + _RETRY_BACKOFF_S
# One call's wall-clock budget: ~17 parcels at the measured pace once the worst-case
# reservation is held back. A job calls this once per 500-parcel GIS batch (a 1,218-
# parcel job adds up to ~9 min); the rest defers to mailing recovery, which calls it
# once per 10-min tick (~100 parcels/hour; 1,245 historical parcels in ~13 h).
# ponytail: one fixed budget for job and recovery; Clark itself allows ~8/min, so
# give recovery its own longer budget if the backlog ever needs to drain faster.
CALL_BUDGET_S = 180.0
_LEASE_WAIT_S = 10.0
# Pages in a row that answered 200 but could not be read. That is the layout
# changing under us, not a bad parcel, so the pass stops instead of spending every
# remaining parcel on it.
_UNPARSED_STREAK_LIMIT = 3

_BLOCK_STATUSES = (403, 429)
_LOCALITY_RE = re.compile(
    r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s*,?\s*(?P<zip>\d{5}(?:-?\d{4})?)?$"
)
# A mailing line that is a deliverable street: a house number, or a box/route form.
# Lines before the first one are addressees ("<NAME> REVOCABLE LIVING TRUST",
# "C/O ..."), which this codebase does not collect.
_STREET_RE = re.compile(
    r"^(?:\d|P\.?\s*O\.?\s*BOX\b|POST OFFICE BOX\b|BOX\b|PMB\b|RR\b|HC\b|GENERAL DELIVERY\b)",
    re.I,
)
_US = {"US", "USA", "UNITED STATES"}


def normalize_parcel(value: object) -> str | None:
    """Digits only, leading zeros dropped; None for anything that is not a parcel."""
    raw = re.sub(r"[\s-]", "", str(value or ""))
    if not raw.isdigit():
        return None
    return raw.lstrip("0") or None


def _field_lines(soup: BeautifulSoup, label: str) -> list[list[str]]:
    """The value lines of every ``td.picBasicInfo1`` whose label starts with ``label``."""
    found = []
    for td in soup.select("td.picBasicInfo1"):
        if not td.get_text(" ", strip=True).startswith(label):
            continue
        span = td.find("span", class_="picBasicInfo2")
        if span is None:
            found.append([])
            continue
        # Lines are <br>-separated. Raw newlines are NOT line breaks: the live page
        # wraps one locality as "BRUSH PRAIRIE WA \n , 98606".
        for br in span.find_all("br"):
            br.replace_with("\x00")
        lines = (" ".join(x.split()) for x in span.get_text().split("\x00"))
        found.append([ln for ln in lines if ln])
    return found


def compose_mailing(lines: list[str]) -> str | None:
    """County mailing lines -> "STREET, CITY, ST ZIP", or None if unreadable."""
    lines = list(lines)
    country = None
    if lines and not any(c.isdigit() for c in lines[-1]) and not _LOCALITY_RE.match(lines[-1]):
        country = lines.pop()
        if country.upper() in _US:
            country = None
    if len(lines) < 2:
        return None
    m = _LOCALITY_RE.match(lines[-1])
    if m:
        locality = f"{m['city'].strip()}, {m['state']}"
        if m["zip"]:
            locality += f" {m['zip']}"
    elif country:
        locality = lines[-1]  # a foreign locality is kept as the county printed it
    else:
        return None
    start = next((i for i, ln in enumerate(lines[:-1]) if _STREET_RE.match(ln)), None)
    if start is None:
        return None
    parts = [*lines[start:-1], locality]
    if country:
        parts.append(country)
    return ", ".join(parts)[:512]  # results.mailing_address is String(512)


def parse_page(html: str, parcel_key: str) -> MailingAnswer:
    """One PIC page -> its answer about ``parcel_key`` (already normalised)."""
    soup = BeautifulSoup(html, "html.parser")
    echoes = _field_lines(soup, "Property Identification Number")
    if not echoes:
        if "No Records Found" in soup.get_text(" "):
            return MailingAnswer(PARCEL_NOT_FOUND)
        return MailingAnswer(UNPARSED)
    if len(echoes) != 1 or not echoes[0] or normalize_parcel(echoes[0][0]) != parcel_key:
        # Never take an address off a page that does not name exactly this parcel.
        return MailingAnswer(PARCEL_MISMATCH)
    mailing = _field_lines(soup, "Owner Mailing Address")
    if len(mailing) != 1:
        return MailingAnswer(UNPARSED)
    if not mailing[0]:
        return MailingAnswer(NONE)
    address = compose_mailing(mailing[0])
    if not address:
        return MailingAnswer(UNPARSED)
    return MailingAnswer(FOUND, mailing_address=address, role="owner")


def _fetch(parcel_key: str, still_held) -> tuple[MailingAnswer, str | None]:
    """(answer, block_reason). A block_reason stops the pass and cools the source.

    A request that went out and failed is REQUEST_FAILED, not source_unavailable: it
    cost Clark a request, so it spends a recovery attempt (Codex P1, round 2). A
    refusal (403/429), or a failure that survives its one retry, also cools the source
    for every process: one call fits ~one slow failure, so a per-call streak would
    never trip during a real outage (Codex P1, round 3).
    """
    reason = "no answer"
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(_RETRY_BACKOFF_S + random.uniform(0, _JITTER_S))  # noqa: S311
            if not still_held():
                # The first request already failed; cool the source anyway so the
                # next lease holder does not walk into the same outage (Codex r4).
                return MailingAnswer(REQUEST_FAILED), reason
        try:
            resp = safe_get(
                _URL, params={"pid": parcel_key, "account": parcel_key},
                headers={"User-Agent": "Mozilla/5.0 (compatible; BridgeLeads)"},
                timeout=_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            reason = type(exc).__name__
            continue
        if resp.status_code in _BLOCK_STATUSES:
            return MailingAnswer(REQUEST_FAILED), f"HTTP {resp.status_code}"
        if resp.status_code == 200:
            return parse_page(resp.text, parcel_key), None
        reason = f"HTTP {resp.status_code}"
        if resp.status_code < 500:
            break  # a 3xx/4xx will not change on a retry
    _logger.warning("clark_pic parcel=%s %s", parcel_key, reason)
    return MailingAnswer(REQUEST_FAILED), reason


def resolve_mailing(parcel_ids: list[str], *, time_budget_s: float = CALL_BUDGET_S
                    ) -> dict[str, MailingAnswer]:
    """Owner mailing addresses for Clark parcels, keyed by CALLER id.

    Every requested id gets an answer. Parcels this call did not reach (budget, lease,
    cooldown, a block mid-pass) are ``source_unavailable``, so the caller defers them.
    Never raises for a source problem.
    """
    from src.scrapers.enrichment.source_admission import SourceAdmission
    from src.scrapers.enrichment.source_health import (
        CLARK_PIC,
        SourceUnavailableError,
        check_source_or_raise,
        record_source_blocked,
    )

    out = {pid: MailingAnswer(SOURCE_UNAVAILABLE) for pid in parcel_ids}
    by_key: dict[str, list[str]] = {}
    for pid in parcel_ids:
        key = normalize_parcel(pid)
        if key is None:
            out[pid] = MailingAnswer(PARCEL_NOT_FOUND)
        else:
            by_key.setdefault(key, []).append(pid)
    if not by_key:
        return out

    counts: dict[str, int] = {}
    deadline = time.monotonic() + time_budget_s
    with SourceAdmission(CLARK_PIC, max_wait_s=_LEASE_WAIT_S) as admission:
        if not admission.holds_lease:
            # Fail CLOSED like pierce_atip_owner: no confirmed lease, no second stream.
            _logger.info("clark_pic: lease not held; %d parcel(s) deferred", len(by_key))
            return out
        try:
            check_source_or_raise(CLARK_PIC)
        except SourceUnavailableError as exc:
            _logger.info("clark_pic: %s; %d parcel(s) deferred", exc, len(by_key))
            return out
        streak = 0
        for key, callers in by_key.items():
            # Paced BEFORE every request, the first one included: the previous lease
            # holder's last request preceded its release, so this keeps the gap
            # fleet-wide across handoffs, not only within one pass (Codex P1).
            if time.monotonic() + _WORST_CASE_FETCH_S > deadline:
                break
            time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
            if not admission.still_held():
                break
            answer, blocked = _fetch(key, admission.still_held)
            counts[answer.outcome] = counts.get(answer.outcome, 0) + 1
            for pid in callers:
                out[pid] = answer  # the parcel that hit the block spent its request too
            if blocked:
                record_source_blocked(CLARK_PIC, f"Property Information Center {blocked}")
                counts["blocked"] = 1
                break
            streak = streak + 1 if answer.outcome == UNPARSED else 0
            if streak >= _UNPARSED_STREAK_LIMIT:
                # Unreadable pages in a row: the layout changed. Stop and cool down
                # rather than spend every remaining parcel on it.
                record_source_blocked(CLARK_PIC, "Property Information Center layout unreadable")
                break
    # Outcome counts only: no owner names, no addresses.
    _logger.info("clark_pic: %d of %d parcel(s) looked up, %s",
                 sum(n for k, n in counts.items() if k != "blocked"), len(by_key), counts)
    return out
