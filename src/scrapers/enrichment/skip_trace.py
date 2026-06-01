"""Skip trace enrichment via Tracerfy (Sprint 4, PRD v1.3).

Batch mode only: scrape jobs enqueue rows into `pending_skip_trace_rows`;
a Celery Beat dispatcher drains them in batches (stays under Tracerfy's
10-POSTs-per-5-min rate limit); Tracerfy delivers results via a webhook;
the webhook receiver downloads the CSV, parses it, upserts phone/email
into the matching `Result` rows.

This module is provider-focused (Tracerfy). The dispatcher/worker glue
lives in `src/workers/skip_trace_dispatcher.py` and the webhook receiver
lives in `src/api/routes/webhooks.py`.

Endpoints used:
- POST /v1/api/trace/          (batch submit, JSON body)
- GET  /v1/api/queue/:id       (optional poll for status)

See docs/vendor/tracerfy-api.md for the full API reference.
"""

import csv
import hashlib
import io
import re
from datetime import UTC, datetime

import requests

from src.config import settings
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get_following

_logger = setup_logger("scraper.enrichment.skip_trace")

# ─── Entity classifier ────────────────────────────────────────────────────────
# Records where the grantor name contains any of these tokens are routed to
# Tracerfy's "advanced" trace (2 credits), which ignores the supplied name
# and identifies the real human owner from the address alone. All other
# records use "normal" trace (1 credit).

_ENTITY_TOKENS = {
    "LLC", "L.L.C", "L.L.C.",
    "INC", "INC.",
    "TRUST", "TRUSTEE",
    # Post-M9 audit: probate scrapers store names like
    # "INGRAM ROY W EST OF" (Estate Of) — add bare EST so those get
    # routed to advanced trace rather than a normal-trace attempt
    # on a deceased person.
    "ESTATE", "EST.", "EST",
    "LP", "L.P.", "L.P",
    "LLP", "L.L.P.",
    "COMPANY", "CO.",
    "CORP", "CORPORATION",
    "PARTNERS",
    "HOLDINGS",
    "ENTERPRISES",
    "PROPERTIES",
    "INVESTMENTS",
    "REALTY",
    "ASSOC", "ASSOCIATION",
    "FOUNDATION",
    "BANK",
    "MORTGAGE",
    "TITLE",
    "INSURANCE",
    # Probate helpers — "EXEC" / "EXECUTOR" appear in personal
    # representative filings ("MCCUNE MYRNA J EXEC"). These
    # identify the estate's representative, not the owner, so
    # route to advanced trace.
    "EXEC", "EXECUTOR", "EXECUTRIX",
    "ADMIN", "ADMINISTRATOR",
    "PR",  # Personal Representative
}


def classify_grantor_as_entity(name: str | None) -> bool:
    """Return True if the grantor name looks like an LLC/Trust/Estate/etc.

    Used to decide between 'normal' and 'advanced' Tracerfy trace_type.
    Advanced trace costs 2 credits instead of 1, but finds the real human
    owner from the address — essential when our scraper returns entity
    names that skip trace providers can't phone/email directly.
    """
    if not name:
        # No grantor → advanced trace lets Tracerfy find the owner
        return True

    upper = name.upper()
    # Check word boundaries so "TRUSTED" doesn't match "TRUST"
    tokens = re.split(r"[,\s\.\(\)]+", upper)
    return any(t in _ENTITY_TOKENS for t in tokens if t)


# ─── Address cache key ────────────────────────────────────────────────────────

def _normalize_address(address: str | None) -> str:
    """Collapse whitespace, uppercase, and strip punctuation for cache keys."""
    if not address:
        return ""
    cleaned = re.sub(r"[\.,#]", " ", address.upper())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def address_cache_key(
    property_address: str | None,
    city: str | None = None,
    state: str | None = None,
) -> str:
    """SHA-256 hash of the normalized (address, city, state) triple.

    Used as the primary key for `skip_trace_cache`. Minor formatting
    variations (punctuation, whitespace, casing) all collapse to the
    same cache key so we don't re-trace a parcel just because the new
    scrape formatted the street differently.
    """
    parts = [
        _normalize_address(property_address),
        _normalize_address(city),
        (state or "").strip().upper(),
    ]
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ─── Name splitter ────────────────────────────────────────────────────────────

def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split a grantor name into (first, last) for Tracerfy normal trace.

    History of this function (read before changing it):

    The original implementation assumed WA recorder portals store
    names as 'LAST FIRST MIDDLE' (which is true for ARMS/LandmarkWeb/
    Helion/AcclaimWeb/EagleWeb). For comma-free 2-token names it
    returned (tokens[1], tokens[0]).

    M9 of the full-SaaS review flagged this as wrong for
    'FIRST LAST' convention outside WA and replaced it with a
    (None, None) return for all comma-free names. That pushed those
    records to advanced trace.

    Post-M9 audit (this session): the (None, None) fallback was
    over-aggressive. A query of 7 days of production data showed
    that ~37% of eligible records have no comma AND look like
    Seattle code violation case descriptions ('LandLord/Tenant ?
    419 21ST AVE'), which are neither personal names nor
    skip-traceable — routing them to advanced just burns more
    credits. The correct policy is:

      1. Reject non-name patterns at a higher layer (this function
         does not see record_type, so the caller must filter).
      2. For genuine personal names, keep the WA-appropriate
         LAST FIRST heuristic for 2-token comma-free names.
      3. For 3+ token comma-free names, give up and return None
         because the split is genuinely ambiguous.

    (2) is restored here. (1) is handled in
    build_pending_row_payload via looks_like_non_personal_party_name.
    """
    if not full_name:
        return None, None

    name = full_name.strip()

    # "LAST, FIRST MIDDLE"  — comma-separated
    if "," in name:
        last, _, rest = name.partition(",")
        first = rest.strip().split()[0] if rest.strip() else None
        return (first or None), (last.strip() or None)

    # Comma-free: use LAST FIRST heuristic ONLY for clean 2-token
    # names. 3+ tokens could be "FIRST MIDDLE LAST" or
    # "LAST FIRST MIDDLE" — too ambiguous, return None.
    tokens = name.split()
    if len(tokens) == 2:
        return tokens[1], tokens[0]
    return None, None


def looks_like_non_personal_party_name(name: str | None) -> bool:
    """Return True when party_name clearly is NOT a human name.

    Code violation scrapers store synthetic values like
    'LandLord/Tenant ? 419 21ST AVE' or 'Weeds ? 1819 HARVARD AVE'
    in party_name — there is no real person to trace. Those records
    should be excluded from skip-trace eligibility entirely, not
    just routed to advanced. This function is the eligibility gate.

    Heuristics:
      - Contains a street-address suffix word (AVE/ST/RD/BLVD/DR/LN
        etc.) — entity/personal names effectively never contain those
      - Contains common non-name punctuation separators (' ? ',
        ' - ') followed by digits (address patterns)
      - Starts with a case-category keyword (LandLord, Weeds,
        Construction, Tenant, Land Use, Tree, Non-licensed, etc.)
    """
    if not name:
        return False
    lower = name.lower()
    # Street-suffix words — a person's legal name does not contain
    # these in practice. Word-boundary match to avoid false-positives
    # on things like 'AVERY' (contains 'ave').
    street_suffixes = (
        " ave", " ave ", " st ", " rd ", " blvd", " dr ", " ln ",
        " way", " pl ", " ct ", " ter ", " pkwy", " cir ",
    )
    if any(s in f" {lower} " for s in street_suffixes):
        return True
    # Case-category prefixes that appear in code_violation party_name
    if any(lower.startswith(p) for p in (
        "landlord", "tenant", "weeds", "construction", "land use",
        "tree", "non-licensed", "project keeps", "noise",
        "nuisance", "derelict",
    )):
        return True
    return False


# ─── Tracerfy batch submit ────────────────────────────────────────────────────

class TracerfyError(Exception):
    """Tracerfy API returned a non-success status or malformed response."""


def submit_batch(
    rows: list[dict],
    trace_type: str = "normal",
    api_token: str | None = None,
) -> dict:
    """POST a batch of rows to Tracerfy's /v1/api/trace/ endpoint.

    Args:
        rows: List of dicts with keys: address, city, state, zip,
              first_name, last_name, mail_address, mail_city, mail_state,
              mailing_zip. For advanced trace only address/city/state are
              required.
        trace_type: "normal" (1 credit/row) or "advanced" (2 credits/row).
        api_token: Override the configured token (mainly for testing).

    Returns:
        Parsed JSON response from Tracerfy. Key fields:
        - queue_id: int — use this to correlate the eventual webhook
        - rows_uploaded: int
        - estimated_wait_seconds: int

    Raises:
        TracerfyError: on any non-200 response or missing queue_id.
    """
    if not rows:
        raise TracerfyError("submit_batch called with empty rows list")

    if trace_type not in ("normal", "advanced"):
        raise TracerfyError(f"Invalid trace_type {trace_type!r}")

    token = api_token or settings.TRACERFY_API_TOKEN
    if not token:
        raise TracerfyError("TRACERFY_API_TOKEN is not configured")

    url = f"{settings.TRACERFY_API_BASE_URL.rstrip('/')}/v1/api/trace/"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    # Tracerfy's batch endpoint docs list both multipart/form-data and
    # application/json, but in practice (verified against live API 2026-04-10)
    # the JSON path returns 415 Unsupported Media Type. Use form-encoded
    # fields, with json_data as a JSON string field — this matches Tracerfy's
    # curl examples and works reliably.
    import json
    json_rows = []
    for r in rows:
        json_rows.append({
            "address": r.get("address") or "",
            "city": r.get("city") or "",
            "state": r.get("state") or "",
            "zip": r.get("zip") or "",
            "first_name": r.get("first_name") or "",
            "last_name": r.get("last_name") or "",
            "mail_address": r.get("mail_address") or "",
            "mail_city": r.get("mail_city") or "",
            "mail_state": r.get("mail_state") or "",
            "mailing_zip": r.get("mailing_zip") or "",
        })

    form_fields = {
        "address_column": "address",
        "city_column": "city",
        "state_column": "state",
        "zip_column": "zip",
        "first_name_column": "first_name",
        "last_name_column": "last_name",
        "mail_address_column": "mail_address",
        "mail_city_column": "mail_city",
        "mail_state_column": "mail_state",
        "mailing_zip_column": "mailing_zip",
        "trace_type": trace_type,
        "json_data": json.dumps(json_rows),
    }

    _logger.info(
        "Tracerfy submit_batch: %d rows, trace_type=%s",
        len(rows), trace_type,
    )

    try:
        # data=form_fields sends application/x-www-form-urlencoded.
        # Tracerfy's doc says multipart/form-data but urlencoded is the
        # standard fallback and works with the same field names.
        resp = requests.post(url, headers=headers, data=form_fields, timeout=30)
    except requests.RequestException as exc:
        raise TracerfyError(f"Network error submitting batch: {exc}") from exc

    if resp.status_code == 429:
        raise TracerfyError(
            "Tracerfy rate limit hit (429). "
            "Dispatcher should back off and retry next tick."
        )
    if resp.status_code >= 400:
        raise TracerfyError(
            f"Tracerfy returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise TracerfyError(f"Tracerfy returned non-JSON: {resp.text[:200]}") from exc

    queue_id = data.get("queue_id")
    if queue_id is None:
        raise TracerfyError(f"Tracerfy response missing queue_id: {data}")

    _logger.info(
        "Tracerfy batch submitted: queue_id=%s rows=%s trace_type=%s wait=%ss",
        queue_id,
        data.get("rows_uploaded"),
        data.get("trace_type"),
        data.get("estimated_wait_seconds"),
    )
    return data


# ─── Webhook ingest ───────────────────────────────────────────────────────────

def _parse_tracerfy_csv(csv_text: str) -> list[dict]:
    """Parse Tracerfy's result CSV into a list of row dicts.

    Normal trace columns (from docs/vendor/tracerfy-api.md):
      address, city, state, mail_address, mail_city, mail_state,
      first_name, last_name,
      primary_phone, primary_phone_type,
      email_1 .. email_5,
      mobile_1 .. mobile_5,
      landline_1 .. landline_3

    Advanced + custom trace add additional owner/mailing fields.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader)


def pick_best_phone(row: dict) -> tuple[str | None, str | None]:
    """Return (phone, phone_type) preferring mobile > primary > landline.

    Tracerfy returns up to 5 mobiles and 3 landlines plus a primary_phone.
    For cold-call use, mobile is strictly better than landline (higher
    answer rate, SMS-capable). We pick in this order:
      1. Mobile-1 (first mobile)
      2. primary_phone if it's Mobile
      3. primary_phone (any type)
      4. Landline-1

    Column naming: Tracerfy's JSON API docs show ``mobile_1`` / ``landline_1``
    (snake_case) but the actual CSV delivered via webhook uses
    ``Mobile-1`` / ``Landline-1`` (title-case with dash separator). We
    handle both for safety.
    """
    def _get(row: dict, *keys: str) -> str:
        for k in keys:
            v = row.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    mobile_1 = _get(row, "Mobile-1", "mobile_1")
    if mobile_1:
        return mobile_1, "Mobile"

    primary = _get(row, "primary_phone")
    primary_type = _get(row, "primary_phone_type") or None
    if primary:
        return primary, primary_type

    landline_1 = _get(row, "Landline-1", "landline_1")
    if landline_1:
        return landline_1, "Landline"

    return None, None


def pick_best_email(row: dict) -> str | None:
    """Return the first non-empty Email-1..5.

    Tracerfy's CSV uses ``Email-1`` / ``Email-2`` / ... (title-case, dash).
    The JSON API docs showed ``email_1`` (snake_case) — we handle both.
    """
    for i in range(1, 6):
        for key in (f"Email-{i}", f"email_{i}"):
            val = (row.get(key) or "").strip()
            if val:
                return val
    return None


def ingest_webhook_csv(csv_text: str) -> list[dict]:
    """Parse a Tracerfy webhook CSV and return a list of normalized hits.

    Each returned dict has:
        address, city, state  — matches PendingSkipTraceRow key
        phone, phone_type, email
        hit: bool (True if any phone OR email was found)

    The caller (webhook endpoint) is responsible for:
      1. Looking up which Result rows match each (address, city, state)
      2. Updating those Result rows' phone/email/status
      3. Writing to skip_trace_cache by address_hash
      4. Marking the matching PendingSkipTraceRow as completed
    """
    rows = _parse_tracerfy_csv(csv_text)
    out: list[dict] = []
    for row in rows:
        phone, phone_type = pick_best_phone(row)
        email = pick_best_email(row)
        out.append({
            "address": (row.get("address") or "").strip(),
            "city": (row.get("city") or "").strip(),
            "state": (row.get("state") or "").strip(),
            "phone": phone,
            "phone_type": phone_type,
            "email": email,
            "hit": bool(phone or email),
            "raw": row,
        })

    hits = sum(1 for h in out if h["hit"])
    phone_hits = sum(1 for h in out if h.get("phone"))
    email_hits = sum(1 for h in out if h.get("email"))
    _logger.info(
        "Tracerfy CSV parsed: %d rows, %d any-hit, %d phone, %d email",
        len(out), hits, phone_hits, email_hits,
    )
    return out


def download_tracerfy_csv(download_url: str) -> str:
    """Fetch the completion CSV from Tracerfy's CDN.

    The webhook payload includes a `download_url` pointing at a public
    DigitalOcean Spaces CDN URL — no auth header required. We still pass
    a polite User-Agent and bound the timeout to be safe.

    SSRF gate: download_url comes from the (signature-verified) Tracerfy
    webhook, but we still fetch it through safe_get_following — which
    validates the initial URL AND every redirect hop (resolve=True, DNS-
    rebinding aware), so the CDN's 302-to-object works while a redirect to a
    private/metadata host is refused. require_allowlisted=False (the CDN host
    is not on the scrape allowlist). The URL may carry a signed token, so it
    is never included in error messages.
    """
    try:
        resp = safe_get_following(
            download_url,
            require_allowlisted=False,
            require_https=True,  # no scheme downgrade — URL may carry a signed token
            headers={"User-Agent": "BridgeLeads/1.0 (+https://bridgeleads.io)"},
            timeout=60,
        )
    except ValueError as exc:
        raise TracerfyError(f"Refusing to download from disallowed URL: {exc}") from exc
    except requests.RequestException as exc:
        raise TracerfyError(f"Failed to download Tracerfy CSV: {exc}") from exc

    if resp.status_code != 200:
        raise TracerfyError(f"Tracerfy CSV download returned {resp.status_code}")

    return resp.text


# ─── Helper: build a PendingSkipTraceRow payload from a Result ─────────────

def build_pending_row_payload(result) -> dict | None:
    """Convert a Result row into the PendingSkipTraceRow kwargs dict.

    Returns None if the Result is not eligible for skip trace:
      - No property_address (can't look up owner without one)
      - party_name is clearly a code-violation case description
        rather than a person/entity (see
        looks_like_non_personal_party_name). Those records would
        burn credits on advanced trace with zero match potential.

    The caller is responsible for the actual insert and for the
    cache lookup before enqueueing.
    """
    if not result.property_address:
        return None

    # Post-M9 audit gate: reject records whose party_name is a
    # code-violation case description ('LandLord/Tenant ? 419 21ST
    # AVE', 'Weeds ? 1819 HARVARD AVE'). Without this filter those
    # records still passed eligibility and were routed to advanced
    # trace, burning ~$0.12 per record for zero hit rate.
    if looks_like_non_personal_party_name(result.party_name):
        return None

    first_name, last_name = split_name(result.party_name)
    is_entity = classify_grantor_as_entity(result.party_name)

    # Advanced trace also handles missing names, so we route to advanced
    # if either (a) the grantor is an entity or (b) we can't split the name.
    trace_type = "advanced" if (is_entity or not (first_name and last_name)) else "normal"

    # Parse property_address into street / city / state if possible.
    # Many of our scrapers concatenate "STREET, CITY, ST ZIP" in one field.
    parsed = _parse_full_address(result.property_address)

    # If property_address only has the street (no city/state), fall back
    # to mailing_address which often has the full "STREET, CITY, ST ZIP".
    # Pierce County GIS returns property_address as street-only and
    # mailing_address as the full format — without this fallback, every
    # skip trace row gets city=None/state=None and errors in Tracerfy.
    if not parsed["city"] and result.mailing_address:
        mail_parsed = _parse_full_address(result.mailing_address)
        if mail_parsed["city"]:
            parsed["city"] = mail_parsed["city"]
            parsed["state"] = mail_parsed["state"]
            parsed["zip"] = mail_parsed["zip"]

    return {
        "job_id": result.job_id,
        "result_id": result.id,
        "user_id": result.user_id,
        "property_address": parsed["street"] or result.property_address,
        "city": parsed["city"],
        "state": parsed["state"],
        "zip": parsed["zip"],
        "first_name": first_name if not is_entity else None,
        "last_name": last_name if not is_entity else None,
        "mail_address": None,  # populated below if mailing_address differs
        "mail_city": None,
        "mail_state": None,
        "mail_zip": None,
        "trace_type": trace_type,
        "status": "queued",
    }


_ADDRESS_RE = re.compile(
    r"^(?P<street>.+?)(?:,\s*(?P<city>[^,]+?))?(?:,\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)?)?$",
    re.IGNORECASE,
)


def _parse_full_address(addr: str) -> dict:
    """Split a combined street address into street / city / state / zip.

    Handles common formats:
        '123 MAIN ST'                                  (1 part)
        '123 MAIN ST, SEATTLE'                         (2 parts)
        '123 MAIN ST, SEATTLE, WA 98101'               (3 parts — state+zip combined)
        '123 MAIN ST, SEATTLE, WA, 98101'              (4 parts — state+zip split)
        '123 MAIN ST, SEATTLE, WA, 98101-1234'         (4 parts — ZIP+4)

    Returns a dict with keys street, city, state, zip — any of which may
    be None if not parseable. Always returns at least `street`.
    """
    result = {"street": None, "city": None, "state": None, "zip": None}
    if not addr:
        return result

    clean = addr.strip().rstrip(",")
    parts = [p.strip() for p in clean.split(",")]

    if len(parts) >= 1:
        result["street"] = parts[0] or None

    if len(parts) == 2:
        # "STREET, CITY" or "STREET, CITY STATE ZIP" (King County GIS format)
        # Try to split "SEATTLE WA 98146" into city/state/zip
        second = parts[1].strip()
        m = re.match(r"^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", second)
        if m:
            result["city"] = m.group(1).strip() or None
            result["state"] = m.group(2)
            result["zip"] = m.group(3)
        else:
            result["city"] = second or None
    elif len(parts) == 3:
        # "STREET, CITY, ST 98101" — state+zip combined in last part
        result["city"] = parts[1] or None
        last = parts[2]
        m = re.match(r"([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", last.upper())
        if m:
            result["state"] = m.group(1)
            result["zip"] = m.group(2) or None
        else:
            m2 = re.match(r"(\d{5}(?:-\d{4})?)", last)
            if m2:
                result["zip"] = m2.group(1)
    elif len(parts) >= 4:
        # "STREET, CITY, ST, 98101"  OR  "STREET, CITY, ST, 98101-1234"
        # Pierce County's mailing_address field uses this format.
        result["city"] = parts[1] or None
        state_part = parts[2].strip().upper()
        zip_part = parts[3].strip()
        # Validate state is 2 uppercase letters
        m_state = re.match(r"^([A-Z]{2})$", state_part)
        if m_state:
            result["state"] = m_state.group(1)
        # Validate zip is 5 or 9 digits (with optional dash)
        m_zip = re.match(r"(\d{5}(?:-\d{4})?)", zip_part)
        if m_zip:
            result["zip"] = m_zip.group(1)

    return result


# ─── Debug helper ─────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(UTC)
