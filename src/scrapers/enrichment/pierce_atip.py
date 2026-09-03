"""Pierce County ATIP address fallback for parcels the GIS layers cannot resolve.

WHY: some Pierce recorder "parcel ids" are personal-property MOBILE HOME accounts
(e.g. 5000050810 — a Notice of Foreclosure by a mobile-home park). They look like
10-digit parcels but have no polygon, so the county Tax_Parcels GIS layer and the
WA statewide parcel layer both return 0 features and the lead ships with a parcel
but no property/mailing address (12 of 217 rows in the 2026-09-02 "Test 2" job).
The Assessor-Treasurer Information Portal (ATIP) — the connector's assessor_url —
carries the site + mailing address for those accounts.

HOW: ATIP's JSON API is gated by reCAPTCHA Enterprise: the SPA sends a
``recaptcha-response`` header on every /api call. Verified live 2026-09-02:
a 2Captcha Enterprise token unlocks ``/api/pcAtipSummary`` over plain HTTP and is
REUSED across requests (server-side verification is cached ~10 min); a missing /
rejected token yields HTTP 200 with an EMPTY body, an unknown parcel yields ``[]``.
Token handling: solve once, reuse until rejected, re-solve ONCE, then stop.

COMPLIANCE (product decision 2026-09-02, recorded here on purpose): the portal
footer cites RCW 42.56.070(8) (no commercial use of LISTS OF INDIVIDUALS). This
module takes ONLY the situs and mailing ADDRESS for a parcel the recorder already
gave us — the same category of data the app already stores from the GIS layer's
Delivery_Address — and NEVER the taxpayer ``name`` (deliberately dropped in
``parse_summary``). Provenance is written to enrichment_data (``address_source``
= ``pierce_atip``, ``atip_account_type``) so downstream consumers can treat a
mobile-home situs differently from fee-simple real property.

SSRF: the host is registered via add_scrape_domain and the parcel is digits-only
and passed as a query PARAM (never interpolated into host/path).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.pierce_atip")

ATIP_HOST = "atip.piercecountywa.gov"
add_scrape_domain(ATIP_HOST)

ATIP_PAGE_URL = f"https://{ATIP_HOST}/app/v2/parcelSearch"
ATIP_SUMMARY_API = f"https://{ATIP_HOST}/api/pcAtipSummary"
ATIP_SITEKEY = "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT"  # public site key (in the page HTML)

MAX_PARCELS = 100          # per call; typical need is ~10/job — keep captcha spend bounded
_DELAY_S = 0.5             # polite delay between lookups
_MAX_HARD_FAILURES = 3     # consecutive non-200 / non-JSON responses -> stop (source down)
_TIMEOUT_S = 20
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DIGITS = re.compile(r"^\d{6,10}$")

# Response classes (see module docstring for the verified server behaviour).
FOUND = "found"
NOT_FOUND = "not_found"
TOKEN_REJECTED = "token_rejected"
HARD_FAILURE = "hard_failure"


def classify_response(status_code: int, body: str) -> tuple[str, list[dict] | None]:
    """Classify one ATIP summary response.

    Only an EXACT empty body (after whitespace strip) on a 200 means the captcha
    token was rejected; ``[]`` is a genuine unknown parcel and must NOT be retried
    (Codex). Anything non-200 or non-JSON is a hard failure.
    """
    if status_code != 200:
        return HARD_FAILURE, None
    if not body or not body.strip():
        return TOKEN_REJECTED, None
    try:
        data = json.loads(body)
    except ValueError:
        return HARD_FAILURE, None
    if not isinstance(data, list):
        return HARD_FAILURE, None
    if not data:
        return NOT_FOUND, None
    return FOUND, [d for d in data if isinstance(d, dict)]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


# The ONLY fields this module is permitted to emit. RCW 42.56.070(8) forbids the
# commercial use of LISTS OF INDIVIDUALS, so ATIP contributes addresses and nothing
# else; the lead's party_name comes from the RECORDER (ARMS), never from here.
_ALLOWED_OUT_KEYS = frozenset(
    {"property_address", "mailing_address", "atip_account_type", "atip_use_code"}
)
# Row keys that carry a person. Read ONLY so their value can be excluded.
# Matched as a SUBSTRING of the key, not an exact list, so an unseen spelling
# (taxpayerName, owner1, mail_name, TaxPayer_NM …) cannot silently bypass the
# boundary just because we never met it (Codex). A statutory guard must fail
# closed against field names we have not seen.
# A key naming a person outright — always a person, whatever else it contains
# ("addressee" also contains "addr", so these must be tested FIRST).
_STRONG_PERSON_KEY_TOKENS = ("taxpayer", "owner", "addressee", "attn")
# "name" on its own is ambiguous: "mail_name" is the addressee, but "street_name"
# and "city_name" are ADDRESS data. Excising one of those would delete a real
# street from mailing_address — the over-broad-guard mistake that once
# false-aborted 14.79% of real rows. An address COMPONENT noun vetoes a bare
# "name"; "mail" deliberately does not, because a mail block's name IS a person.
_ADDRESS_COMPONENT_KEY_TOKENS = ("street", "addr", "situs", "city", "state", "zip", "line")


def _is_person_key(key: object) -> bool:
    """Is this row key a PERSON field (so its value must be excluded)?

    The address-component veto applies to EVERY person token, not just a bare
    "name". An `owner_address` / `owner_city` / `taxpayer_addr` key is ADDRESS
    data that merely mentions the owner; treating it as a person harvested a
    STREET into the exclusion list and then deleted that street from
    mailing_address (measured: mail became "PUYALLUP, WA, 98372"). Only
    "addressee"/"attn" outrank the veto, since "addressee" itself contains "addr".
    """
    low = str(key).lower()
    if "addressee" in low or "attn" in low:
        return True
    if any(tok in low for tok in _ADDRESS_COMPONENT_KEY_TOKENS):
        return False
    return any(tok in low for tok in _STRONG_PERSON_KEY_TOKENS) or "name" in low
# Prefixes an assessor puts before an addressee; stripped before comparing so
# "C/O JANE DOE" is recognised as the same person as "JANE DOE".
_ADDRESSEE_PREFIX_RE = re.compile(r"^\s*(?:C\s*/\s*O|C/O|ATTN\.?:?|ATTENTION:?)\s+", re.IGNORECASE)


def _person_values(row: dict) -> list[str]:
    out = []
    for key, value in row.items():
        if not _is_person_key(key):
            continue
        val = _clean(value)
        if val:
            out.append(_ADDRESSEE_PREFIX_RE.sub("", val).upper())
    return out


def _drop_person_line(mailing: str | None, row: dict) -> str | None:
    """Remove an addressee NAME that the assessor put in the mail block.

    Verified against production 2026-09-03: all 31 ATIP-enriched rows carry a real
    street in mail/mail2/mail3 and no name, so this strips nothing today. It exists
    because an assessor mailing block conventionally MAY lead with the addressee,
    and that would put a person's name into mailing_address without anyone ever
    reading row["name"].
    """
    if not mailing:
        return mailing
    people = _person_values(row)
    if not people:
        return mailing
    kept = []
    for seg in mailing.split(", "):
        # Strip an addressee prefix for COMPARISON only. Whole-segment equality
        # alone missed "C/O JANE DOE" and, worse, "JANE DOE 10608 63RD STREET CT E",
        # where the assessor puts the name and the street on ONE line (Codex).
        bare = _ADDRESSEE_PREFIX_RE.sub("", seg).strip()
        if bare.upper() in people:
            continue
        # A segment that is NOT a person passes through byte-for-byte: rebuilding
        # it from the prefix-stripped form would silently rewrite a legitimate
        # "C/O ..." mailing line.
        cleaned, upper = seg, bare.upper()
        for person in people:
            if not person:
                continue
            # WORD-BOUNDARY match, never a bare substring: a plain `in` test
            # excised the name "LEE" out of the street "LEELAND ST" and produced
            # "123 LAND ST" — a FABRICATED address, which is worse than the leak
            # this guard exists to prevent.
            m = re.search(
                r"(?<![A-Z0-9])" + re.escape(person) + r"(?![A-Z0-9])", upper
            )
            if m:
                # Name shares the line with the street: excise only the name.
                cleaned = (bare[:m.start()] + bare[m.end():]).strip(" ,")
                bare, upper = cleaned, cleaned.upper()
        if cleaned:
            kept.append(cleaned)
    return ", ".join(kept) if kept else None


def _assert_address_only(out: dict) -> None:
    """Fail loudly if this module ever emits anything but address data."""
    extra = set(out) - _ALLOWED_OUT_KEYS
    if extra:
        raise AssertionError(
            f"pierce_atip may only emit address fields; refusing to return {sorted(extra)} "
            "(RCW 42.56.070(8) boundary — see module docstring)"
        )


def parse_summary(row: dict) -> dict[str, str | None] | None:
    """Map one ATIP summary row to the app's address fields.

    Returns None when the row has no situs (nothing usable). The taxpayer ``name``
    is deliberately NOT returned (compliance boundary — see module docstring).
    Mailing is built in the same shape the GIS path stores
    ("<street>, <CITY>, <ST>, <ZIP>") so downstream parsing sees one format.
    """
    situs = _clean(row.get("situs"))
    if not situs:
        return None
    street_parts = [p for p in (_clean(row.get("mail")), _clean(row.get("mail2")), _clean(row.get("mail3"))) if p]
    city = _clean(row.get("city"))
    state = _clean(row.get("state"))
    zipcode = _clean(row.get("zip"))
    mailing: str | None = None
    if street_parts:
        parts = list(street_parts)
        locality = ", ".join(p for p in (city, state) if p)
        if locality:
            parts.append(locality)
        if zipcode:
            parts.append(zipcode)
        mailing = ", ".join(parts)
    mailing = _drop_person_line(mailing, row)
    out = {
        "property_address": situs,
        "mailing_address": mailing,
        "atip_account_type": _clean(row.get("acct_type")),
        "atip_use_code": _clean(row.get("use_cd")),
    }
    # HARD GUARD, not a comment: the RCW 42.56.070(8) boundary is enforced here so
    # a later edit cannot quietly widen it. Previously the boundary held only
    # because nothing happened to read row["name"].
    _assert_address_only(out)
    return out


def _solve_token() -> str | None:
    from src.scrapers.enrichment.captcha import solve_recaptcha

    return asyncio.run(solve_recaptcha(ATIP_PAGE_URL, ATIP_SITEKEY, enterprise=True))


def _fetch(parcel: str, token: str):
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": ATIP_PAGE_URL,
        "recaptcha-response": token,
    }
    return safe_get(ATIP_SUMMARY_API, params={"iParcelNumber": parcel}, headers=headers, timeout=_TIMEOUT_S)


def lookup_atip_addresses(
    parcel_ids: list[str], *, max_parcels: int = MAX_PARCELS
) -> tuple[dict[str, dict[str, str | None]], dict[str, int]]:
    """Resolve site + mailing addresses for Pierce parcels via ATIP.

    Returns ``(results, stats)``: results keyed by the caller's parcel string
    (only parcels with a situs), and counters ``attempted / resolved / not_found /
    token_rejected / hard_failure / solves`` for the job log. Best-effort: never
    raises for a source problem; returns what it has.
    """
    stats = {"attempted": 0, "resolved": 0, "not_found": 0, "token_rejected": 0,
             "hard_failure": 0, "solves": 0, "skipped_cap": 0}
    results: dict[str, dict[str, str | None]] = {}
    if not settings.CAPTCHA_ENABLED or not settings.CAPTCHA_API_KEY:
        _logger.info("ATIP lookup skipped: CAPTCHA solving disabled")
        return results, stats

    wanted: list[str] = []
    seen: set[str] = set()
    for raw in parcel_ids:
        pid = re.sub(r"\D", "", raw or "")
        if _DIGITS.match(pid) and pid not in seen:
            seen.add(pid)
            wanted.append(pid)
    if len(wanted) > max_parcels:
        stats["skipped_cap"] = len(wanted) - max_parcels
        _logger.warning("ATIP lookup capped at %d/%d parcels", max_parcels, len(wanted))
        wanted = wanted[:max_parcels]
    if not wanted:
        return results, stats

    from src.scrapers.enrichment.captcha import invalidate_token

    token = _solve_token()
    stats["solves"] += 1
    if not token:
        _logger.warning("ATIP lookup aborted: captcha solve failed")
        return results, stats

    resolved_once = False
    consecutive_hard = 0
    by_digits = {re.sub(r"\D", "", p or ""): p for p in parcel_ids}
    for pid in wanted:
        stats["attempted"] += 1
        try:
            resp = _fetch(pid, token)
            kind, rows = classify_response(resp.status_code, resp.text)
            if kind == TOKEN_REJECTED and not resolved_once:
                # One re-solve for the whole batch: the server-side verification
                # cache is ~10 min, so a second rejection means we cannot proceed.
                stats["token_rejected"] += 1
                invalidate_token(ATIP_SITEKEY, ATIP_PAGE_URL, enterprise=True)
                token = _solve_token()
                stats["solves"] += 1
                resolved_once = True
                if not token:
                    break
                resp = _fetch(pid, token)
                kind, rows = classify_response(resp.status_code, resp.text)
        except Exception as exc:  # noqa: BLE001 — a source hiccup must not fail enrichment
            _logger.warning("ATIP lookup error for parcel %s: %s", pid, str(exc)[:100])
            kind, rows = HARD_FAILURE, None

        if kind == TOKEN_REJECTED:
            stats["token_rejected"] += 1
            _logger.warning("ATIP token rejected after re-solve — stopping this batch")
            break
        if kind == HARD_FAILURE:
            stats["hard_failure"] += 1
            consecutive_hard += 1
            if consecutive_hard >= _MAX_HARD_FAILURES:
                _logger.warning("ATIP: %d consecutive failures — source may be down, stopping", consecutive_hard)
                break
        else:
            consecutive_hard = 0
            if kind == NOT_FOUND:
                stats["not_found"] += 1
            else:
                parsed = parse_summary(rows[0]) if rows else None
                if parsed:
                    results[by_digits.get(pid, pid)] = parsed
                    stats["resolved"] += 1
                else:
                    stats["not_found"] += 1
        time.sleep(_DELAY_S)

    _logger.info("ATIP lookup: %s", stats)
    return results, stats
