"""Owner mailing addresses from the Thurston County Assessor "A+" parcel page.

Thurston is an EagleWeb recorder county with no mailing source: its parcels fall to
the WA statewide situs-only layer and ``mailing_address`` stays NULL (county_gis.py).
The assessor publishes, per parcel and with no session, JS, CAPTCHA or token:

    GET https://tcproperty.co.thurston.wa.us/propsql/basic_p.asp?pn=<11 digits>

    <td class='emphatic'>Parcel Number: 74700001201</td>
    <td class='emphatic'>Situs Address:</td><td>1418 COLLEGE ST SE</td>
    <td class='emphatic'>Owner:</td><td>...</td>
    <td class='emphatic'>Address:</td><td>3000 PACIFIC AVE SE</td>
    <td>&nbsp;</td><td>OLYMPIA, WA   98501</td>
    <td class='emphatic'>Taxpayer:</td><td>...</td>
    <td class='emphatic'>Address:</td><td>3000 PACIFIC AVE SE</td>
    <td>&nbsp;</td><td>OLYMPIA, WA   98501</td>

(verified 2026-10-02; the fixture in tests/fixtures is that page, names redacted).
The TAXPAYER address is the one taken: it is where the county sends the tax bill,
which is what a mailing address means in this product. The owner block is read
only when the page has no taxpayer block. The page's own "Parcel Number:" must echo
the parcel asked for, or nothing is taken (``parcel_mismatch``).

Same contract and guards as pacs_parcel.py: settled outcomes (found / none /
parcel_not_found / parcel_mismatch) versus retryable ones (unparsed /
request_failed / source_unavailable); one paced stream fleet-wide; a 403/429 cools
the source. No terms on the page restrict use (the search form carries no
disclaimer; the page says only that accuracy is not guaranteed), so this source is
not behind the restricted-mailing kill switch.
"""
from __future__ import annotations

import random
import re
import time
from functools import partial

import requests
from bs4 import BeautifulSoup

from src.scrapers.enrichment.pacs import compose_pacs_mailing
from src.scrapers.enrichment.pacs_parcel import (
    NONE,
    PARCEL_MISMATCH,
    PARCEL_NOT_FOUND,
    REQUEST_FAILED,
    UNPARSED,
    _request,
)
from src.scrapers.enrichment.snohomish_assessor_roll import (
    FOUND,
    SOURCE_UNAVAILABLE,
    MailingAnswer,
)
from src.utils.logger import setup_logger
from src.utils.pinned_http import pinned_session

_logger = setup_logger("enrichment.thurston_assessor")

SOURCE = "thurston_assessor"
_URL = "https://tcproperty.co.thurston.wa.us/propsql/basic_p.asp"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
_PACE_S = 3.0
_JITTER_S = 1.0
_TIMEOUT_S = 25
_RETRY_BACKOFF_S = 8.0
CALL_BUDGET_S = 240.0
_LEASE_WAIT_S = 10.0
_UNPARSED_STREAK_LIMIT = 3
_PARCEL_RE = re.compile(r"Parcel Number:\s*([0-9]{5,20})")


def normalize_parcel(value: object) -> str | None:
    """Thurston parcels are 11 digits with no separators; accept formatting noise."""
    raw = re.sub(r"[\s.\-]", "", str(value or ""))
    if not raw.isdigit():
        return None
    return raw.lstrip("0") or None


def _http_get(session: requests.Session, parcel_key: str) -> requests.Response:
    return session.get(_URL, params={"pn": parcel_key}, timeout=_TIMEOUT_S, allow_redirects=False)


def _blocks(soup: BeautifulSoup) -> dict[str, list[str]]:
    """{"owner": lines, "taxpayer": lines} from the labelled rows.

    An "Address:" row belongs to the nearest preceding "Owner:" or "Taxpayer:"
    label; the locality sits in the next row's second cell (first cell blank).
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(" ", strip=True).rstrip(":").lower()
        value = " ".join(tds[1].get_text(" ").split()) if len(tds) >= 2 else ""
        if label in ("owner", "taxpayer"):
            current = label
            out.setdefault(current, [])
        elif current is None:
            continue
        elif label == "address":
            if value:
                out[current].append(value)
        elif label == "" and value and out[current]:
            out[current].append(value)  # the locality row under an address line
        elif label:
            current = None  # any other label ("Abbreviated Legal:") ends the block
    return out


def parse_page(html: str, parcel_key: str) -> MailingAnswer:
    """One A+ page -> its answer about ``parcel_key`` (already normalised)."""
    soup = BeautifulSoup(html, "html.parser")
    echoes = _PARCEL_RE.findall(soup.get_text(" "))
    if not echoes:
        text = soup.get_text(" ").lower()
        if "no record" in text or "not found" in text or "no parcel" in text:
            return MailingAnswer(PARCEL_NOT_FOUND)
        return MailingAnswer(UNPARSED)
    if len(set(echoes)) != 1 or normalize_parcel(echoes[0]) != parcel_key:
        return MailingAnswer(PARCEL_MISMATCH)
    blocks = _blocks(soup)
    lines = blocks.get("taxpayer")
    role = "taxpayer"
    if lines is None:
        lines = blocks.get("owner")
        role = "owner"
    if lines is None:
        return MailingAnswer(UNPARSED)
    if not lines:
        return MailingAnswer(NONE)
    address = compose_pacs_mailing(lines)
    if not address:
        return MailingAnswer(UNPARSED)
    return MailingAnswer(FOUND, mailing_address=address, role=role)


def resolve_mailing(parcel_ids: list[str], *, time_budget_s: float = CALL_BUDGET_S
                    ) -> dict[str, MailingAnswer]:
    """Taxpayer mailing addresses for Thurston parcels, keyed by CALLER id.

    Every requested id gets an answer; parcels this call did not reach are
    ``source_unavailable`` so the caller defers them. Never raises for a source
    problem.
    """
    from src.scrapers.enrichment.source_admission import SourceAdmission
    from src.scrapers.enrichment.source_health import (
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
    reserve = _PACE_S + _JITTER_S + _TIMEOUT_S
    with SourceAdmission(SOURCE, max_wait_s=_LEASE_WAIT_S) as admission:
        if not admission.holds_lease:
            _logger.info("%s: lease not held; %d parcel(s) deferred", SOURCE, len(by_key))
            return out
        try:
            check_source_or_raise(SOURCE)
        except SourceUnavailableError as exc:
            _logger.info("%s: %s; %d parcel(s) deferred", SOURCE, exc, len(by_key))
            return out
        session = pinned_session()
        session.headers.update(_HEADERS)
        streak = 0
        for i, (key, callers) in enumerate(by_key.items()):
            if i and time.monotonic() + reserve > deadline:
                break
            time.sleep(_PACE_S + random.uniform(0, _JITTER_S))  # noqa: S311
            if not admission.still_held():
                break
            resp, blocked = _request(partial(_http_get, session, key), admission.still_held)
            if blocked:
                record_source_blocked(SOURCE, f"Thurston A+ {blocked}")
                _logger.warning("%s blocked (%s); pass stopped", SOURCE, blocked)
                break
            answer = parse_page(resp.text, key) if resp is not None else MailingAnswer(REQUEST_FAILED)
            for pid in callers:
                out[pid] = answer
            counts[answer.outcome] = counts.get(answer.outcome, 0) + 1
            streak = streak + 1 if answer.outcome == UNPARSED else 0
            if streak >= _UNPARSED_STREAK_LIMIT:
                _logger.error("%s: %d unparsed pages in a row; layout drift, pass stopped", SOURCE, streak)
                break
    _logger.info("%s: %d parcel(s) requested, outcomes %s", SOURCE, len(by_key), counts)
    return out
