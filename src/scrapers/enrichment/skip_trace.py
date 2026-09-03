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

from src.api.middleware.security import validate_scraping_target
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
    # Estate/proxy descriptors — not a directly-traceable living owner.
    "HEIRS", "ATTORNEY", "ATTY",
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
    user_id: str,
    property_address: str | None,
    city: str | None = None,
    state: str | None = None,
) -> str:
    """SHA-256 hash of (user_id, normalized address, city, state).

    Used as the primary key for `skip_trace_cache`. The cache is PER-TENANT:
    `user_id` is part of the hash, so the same address scraped by two different
    tenants maps to two different keys — tenant B never reads the skip-traced PII
    that tenant A paid Tracerfy to source (cross-tenant reuse decision, 2026-06-10).
    A tenant re-scraping its OWN address still hits its own cache (cost-saving
    within a tenant preserved). Minor formatting variations (punctuation,
    whitespace, casing) still collapse to the same key for a given tenant.
    """
    parts = [
        str(user_id),
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


# Name-suffix tokens that legitimately trail a person name, so "SMITH JOHN JR"
# is still a confident person parse (last=SMITH, first=JOHN).
_PERSON_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _is_initial_or_suffix(token: str) -> bool:
    """True if a token is a middle initial ('H', 'A.') or a name suffix
    ('JR', 'III'). Used to confirm a 'LAST FIRST ...' name where every token
    after FIRST is just an initial/suffix (so the order is unambiguous)."""
    t = token.rstrip(".").upper()
    return len(t) == 1 or t in _PERSON_SUFFIXES


def _parse_wa_recorder_person(owner: str | None) -> tuple[str | None, str | None]:
    """Best-effort (first, last) for ONE owner segment using the WA recorder
    'LAST FIRST [MIDDLE]' convention. Returns (None, None) unless it parses as a
    confident person.

    This is intentionally NOT a general name splitter — it assumes WA recorder
    ordering (LandmarkWeb/ARMS), so it is only called from select_traceable_owner
    on recorder party_name data. split_name() stays conservative for other
    callers (e.g. non-WA), per Codex review.
    """
    if not owner:
        return None, None
    name = owner.strip()
    if not name or classify_grantor_as_entity(name):
        return None, None
    if "," in name:  # "LAST, FIRST [MIDDLE]"
        last, _, rest = name.partition(",")
        first = rest.strip().split()[0] if rest.strip() else None
        return (first or None), (last.strip() or None)
    tokens = name.split()
    # WA "LAST FIRST [initials/suffixes]": confident only when every token after
    # FIRST is a middle-initial or suffix ("WALKER WILLIAM H III", "BAUS DONALD
    # L"). This rejects "STEPHEN P MYERS" (FIRST M LAST) because the trailing
    # token isn't an initial/suffix -> ambiguous -> advanced.
    if len(tokens) >= 2 and all(_is_initial_or_suffix(t) for t in tokens[2:]):
        return tokens[1], tokens[0]
    return None, None


def _owner_confidence(owner: str) -> int:
    """Rank one owner segment as a person-name candidate (higher = more
    confident). -1 = not usable as a person (entity, estate, or ambiguous)."""
    n = owner.strip()
    if not n or classify_grantor_as_entity(n):
        return -1
    if "," in n:
        return 3
    tokens = n.split()
    if len(tokens) == 2:
        return 2
    if len(tokens) >= 3 and all(_is_initial_or_suffix(t) for t in tokens[2:]):
        return 1
    return -1


def select_traceable_owner(party_name: str | None) -> tuple[str | None, str | None]:
    """Choose the highest-confidence PERSON owner from a (possibly multi-owner,
    ' / '-separated) party_name for a Tracerfy NORMAL trace (name + address).

    Returns (first, last) for a confident person, else (None, None) so the caller
    falls back to ADVANCED (address-only). Conservative by design (Codex review):
    for cold-outreach data, accuracy/compliance beats the 1-credit saving, so
    entities, estates/trusts, and ambiguous 3+-full-token names yield no
    candidate. Owners are ranked (comma 'LAST, FIRST' > 2-token 'LAST FIRST' >
    3-token-with-initial/suffix) so a clean person beside an entity trustee/bank
    (e.g. "BOYLE DAVID E / QUALITY LOAN SERVICE CORP") is still found.
    """
    if not party_name:
        return None, None
    owners = [o.strip() for o in party_name.split(" / ") if o.strip()] or [party_name.strip()]
    best, best_rank = None, 0
    for o in owners:
        r = _owner_confidence(o)
        if r > best_rank:
            best, best_rank = o, r
    if best is None:
        return None, None
    return _parse_wa_recorder_person(best)


def looks_like_non_personal_party_name(name: str | None) -> bool:
    """Return True when party_name clearly is NOT a human name.

    Code violation scrapers store synthetic values like
    'LandLord/Tenant ? 419 21ST AVE' or 'Weeds ? 1819 HARVARD AVE'
    in party_name — there is no real person to trace. Those records
    should be excluded from skip-trace eligibility entirely, not
    just routed to advanced. This function is the eligibility gate.

    This gate SUPPRESSES skip trace entirely (build_pending_row_payload
    returns None), so it must only fire on shapes that are provably not a
    party. It is NOT the entity router — an LLC/trust named after its
    street ("1423 1ST AVE LLC") still owns a real property, and the advanced
    (address-only) trace ignores the name, so those must pass through.

    Heuristics (any one → True):
      - Starts with a case-category keyword (LandLord, Weeds,
        Construction, Tenant, Land Use, Tree, Non-licensed, etc.)
      - A separator (' ? ', ' - ') followed by a house number — the
        "<category> ? <address>" case-description shape
      - The whole name IS a street address: leading house number + a
        street-suffix WORD, with no entity token (LLC/INC/TRUST/…)

    History: the first version substring-matched " ave"/" way"/… against
    the padded name, which classified real people — AVELINO, AVERY,
    WAYNE, WAYLAND — as "not a person" and silently dropped them from
    skip trace (43 rows / 12 jobs in 90 days of prod, 2026-09-02).
    Street suffixes are now matched as whole words only.
    """
    if not name:
        return False
    lower = name.lower().strip()
    # Case-category prefixes that appear in code_violation party_name
    if any(lower.startswith(p) for p in (
        "landlord", "tenant", "weeds", "construction", "land use",
        "tree", "non-licensed", "project keeps", "noise",
        "nuisance", "derelict",
    )):
        return True
    # "<category> ? 419 21ST AVE" / "<category> - 1819 HARVARD AVE"
    if _CASE_SEPARATOR_RE.search(name):
        return True
    # The party name is itself a street address (house number … suffix word)
    # and carries no entity token that would make it a nameable owner.
    if _ADDRESS_SHAPED_NAME_RE.match(name) and not classify_grantor_as_entity(name):
        return True
    return False


# "<category> ? <house number>" — the code-violation case-description shape.
_CASE_SEPARATOR_RE = re.compile(r"\s[?\-]\s*\d")
# Leading house number … whole-word street suffix (optionally followed by a
# post-directional / unit). Whole-word so "WAYNE"/"AVERY"/"AVELINO" never match.
_ADDRESS_SHAPED_NAME_RE = re.compile(
    r"^\s*\d{1,6}[A-Z]?\s+\S.*?\b("
    r"AVE|AVENUE|ST|STREET|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|WAY|LN|LANE|CT|COURT|"
    r"PL|PLACE|HWY|HIGHWAY|TER|TERRACE|CIR|CIRCLE|LOOP|PKWY|PARKWAY|SQ|SQUARE|TRL|TRAIL"
    r")\b",
    re.IGNORECASE,
)


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

    # S4: SSRF defense-in-depth for this server-side POST. TRACERFY_API_BASE_URL
    # is operator config; validate it (resolve=True, DNS-rebinding aware) and
    # require HTTPS before sending the bearer token so a misconfigured/poisoned
    # base URL can't exfiltrate the token to an internal/metadata host. The
    # POST below uses a trust_env=False session with allow_redirects=False
    # (safe_http is GET-only; this mirrors its guarantees for the POST path).
    if not url.lower().startswith("https://"):
        raise TracerfyError("TRACERFY_API_BASE_URL must use HTTPS")
    try:
        validate_scraping_target(url, require_allowlisted=False, resolve=True)
    except ValueError as ssrf_exc:
        raise TracerfyError(f"Refusing unsafe Tracerfy endpoint: {ssrf_exc}") from ssrf_exc

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
        # S4: trust_env=False (no ambient proxy reroute) + allow_redirects=False
        # (a poisoned 302 can't bounce the token-bearing POST to an internal
        # host). Validation above already ran resolve=True on this URL.
        _sess = requests.Session()
        _sess.trust_env = False
        resp = _sess.post(
            url, headers=headers, data=form_fields, timeout=30, allow_redirects=False
        )
    except requests.ConnectionError as exc:
        # Never delivered (refused / DNS / reset before a response) — the
        # dispatcher may safely release the batch and retry next tick.
        raise TracerfyError(f"Connection error submitting batch: {exc}") from exc
    except requests.RequestException as exc:
        # Read timeout etc.: Tracerfy MAY have accepted the batch. The dispatcher
        # treats this as unknown outcome and never auto-resubmits (double-pay).
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


def _phone_digits(p: str | None) -> str:
    """Last-10 national digits of a phone, for de-duping the same number that
    appears under multiple Tracerfy columns. Returns '' if not enough digits."""
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def pick_phones(row: dict, n: int = 3) -> list[dict]:
    """Return up to n DISTINCT phones as [{"number", "type"}], best-first.

    Tracerfy returns up to 5 mobiles + 3 landlines + a primary_phone. For
    cold-call/SMS use mobile beats landline, so the order is:
      Mobile-1..5 → primary_phone (if not a dup of a mobile) → Landline-1..3.
    Dedups by normalized digits but preserves the original display value.
    Column naming: the JSON API docs show ``mobile_1``/``landline_1`` (snake)
    but the webhook CSV uses ``Mobile-1``/``Landline-1`` (title-dash) — accept both.
    """
    def _get(*keys: str) -> str:
        for k in keys:
            v = row.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    primary_num = _get("primary_phone")
    primary_type = _get("primary_phone_type") or None
    m1 = _get("Mobile-1", "mobile_1")
    l1 = _get("Landline-1", "landline_1")

    # phones[0] MUST equal the legacy pick_best_phone exactly (Mobile-1 ->
    # primary_phone -> Landline-1, checking Mobile-1 *specifically*, never a
    # later mobile column) so the scalar `phone` used by dialer/export/cache
    # does not drift. Extras then follow in mobile -> primary -> landline order.
    legacy: tuple[str, str | None] | None = None
    if m1:
        legacy = (m1, "Mobile")
    elif primary_num:
        legacy = (primary_num, primary_type)
    elif l1:
        legacy = (l1, "Landline")

    candidates: list[tuple[str, str | None]] = []
    if legacy:
        candidates.append(legacy)
    for i in range(1, 6):
        m = _get(f"Mobile-{i}", f"mobile_{i}")
        if m:
            candidates.append((m, "Mobile"))
    if primary_num:
        candidates.append((primary_num, primary_type))
    for i in range(1, 4):
        ll = _get(f"Landline-{i}", f"landline_{i}")
        if ll:
            candidates.append((ll, "Landline"))

    out: list[dict] = []
    seen: set[str] = set()
    for number, ptype in candidates:
        key = _phone_digits(number)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"number": number, "type": ptype})
        if len(out) >= n:
            break
    return out


def pick_emails(row: dict, n: int = 3) -> list[str]:
    """Return up to n DISTINCT emails (Email-1..5 / email_1..5), best-first,
    deduped case-insensitively (original case preserved)."""
    out: list[str] = []
    seen: set[str] = set()
    for i in range(1, 6):
        for key in (f"Email-{i}", f"email_{i}"):
            v = (row.get(key) or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
                break
        if len(out) >= n:
            break
    return out


def pick_best_phone(row: dict) -> tuple[str | None, str | None]:
    """Primary phone = the first of pick_phones() — a thin wrapper so the
    single-column value can never drift from phones[0]."""
    ps = pick_phones(row, 1)
    return (ps[0]["number"], ps[0]["type"]) if ps else (None, None)


def pick_best_email(row: dict) -> str | None:
    """Primary email = the first of pick_emails() (wrapper; see pick_best_phone)."""
    es = pick_emails(row, 1)
    return es[0] if es else None


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
        # Multi-contact: up to 3 each. The single phone/email stay the PRIMARY
        # (phones[0]/emails[0]) so every existing consumer (.phone/.email:
        # dialer push, CSV export, segments, cache, reuse) is unchanged.
        phones = pick_phones(row, 3)
        emails = pick_emails(row, 3)
        phone = phones[0]["number"] if phones else None
        phone_type = phones[0]["type"] if phones else None
        email = emails[0] if emails else None
        out.append({
            "address": (row.get("address") or "").strip(),
            "city": (row.get("city") or "").strip(),
            "state": (row.get("state") or "").strip(),
            "phone": phone,
            "phone_type": phone_type,
            "email": email,
            "phones": phones,
            "emails": emails,
            "hit": bool(phones or emails),
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
        # M2: never interpolate the requests exception — its string embeds the
        # full URL, and the Tracerfy CDN download_url carries a signed token
        # (X-Amz-* style) that must not reach logs. Surface the type only, and
        # use `from None` so a downstream traceback (Celery autoretry) can't
        # print the original exception's URL via the __cause__ chain.
        raise TracerfyError(
            f"Failed to download Tracerfy CSV: {type(exc).__name__}"
        ) from None

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
    # Tracerfy traces by PROPERTY address; a blank or the legacy
    # "(enrichment unavailable)" placeholder is not one (lead_actionability).
    prop = (result.property_address or "").strip()
    if not prop or prop == "(enrichment unavailable)":
        return None

    # Post-M9 audit gate: reject records whose party_name is a
    # code-violation case description ('LandLord/Tenant ? 419 21ST
    # AVE', 'Weeds ? 1819 HARVARD AVE'). Without this filter those
    # records still passed eligibility and were routed to advanced
    # trace, burning ~$0.12 per record for zero hit rate.
    if looks_like_non_personal_party_name(result.party_name):
        return None

    # Phase 2: pick the highest-confidence PERSON owner (handles multi-owner
    # " / " names, picking the clean person beside an entity trustee/bank, and
    # WA "LAST FIRST M" 3-token names). Normal trace (1 credit, name+address)
    # only when confident; otherwise advanced (2 credits, address-only) lets
    # Tracerfy find the owner. Conservative by design — accuracy/compliance over
    # the credit saving for cold outreach.
    first_name, last_name = select_traceable_owner(result.party_name)
    trace_type = "normal" if (first_name and last_name) else "advanced"

    # Parse property_address into street / city / state if possible.
    # Many of our scrapers concatenate "STREET, CITY, ST ZIP" in one field.
    parsed = _parse_full_address(result.property_address)

    # property_address is street-only from several sources (Pierce county GIS,
    # the WA statewide layer, the King assessor), so the locality has to come
    # from somewhere else or Tracerfy errors the row out. Two fallbacks, in
    # descending order of truth:
    #
    #   1. The STRUCTURED SITUS parts (migration 085): property_city /
    #      property_state / property_zip describe the PROPERTY itself, which is
    #      exactly what Tracerfy keys on. Before #188 the statewide layer faked
    #      a mailing address out of the situs and this fell out of fallback 2 by
    #      accident; #188 correctly stopped fabricating that mailing line but
    #      nothing read the new columns, so statewide-enriched rows reached
    #      Tracerfy with city/state/zip all None (Codex, 2026-09-03).
    #   2. The owner's mailing_address — only a proxy for where the property is
    #      (it is right for owner-occupied, wrong for absentee), so it is the
    #      last resort, not the first.
    #
    # Each field is filled INDEPENDENTLY: a row can carry a parsed city but no
    # state/zip, and gating the whole block on a missing city would strand them
    # (Codex). No default state — "WA" is real source context that _situs_parts
    # stores for the WA layer, not an assumption skip trace may invent.
    for _field, _stored in (
        ("city", getattr(result, "property_city", None)),
        ("state", getattr(result, "property_state", None)),
        ("zip", getattr(result, "property_zip", None)),
    ):
        if not parsed[_field] and _stored:
            parsed[_field] = str(_stored).strip() or None

    # The owner's mailing address is also a separate Tracerfy input (mail_*
    # columns) that improves normal-trace matching when the owner does not live
    # at the property. Parse it once; reuse it for the locality fallback.
    #
    # This fallback stays ATOMIC — all three fields from the mailing parse, or
    # none — unlike the situs fill above. The situs parts describe the same
    # property as property_address, so blending them per-field is coherent; the
    # mailing address is a DIFFERENT place whenever the owner is absentee.
    # Taking a city from the situs and a ZIP from the mailing line would invent
    # a locality that exists nowhere ("OLYMPIA WA 98101" for a Seattle-mailed
    # Olympia property) — the same fabricate-an-address class of bug #188 was
    # fixing. Only when we still have NO city at all is the owner's mail worth
    # guessing from, and then it is used whole.
    mail_parsed = _parse_full_address(result.mailing_address) if result.mailing_address else None
    if not parsed["city"] and mail_parsed and mail_parsed["city"]:
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
        # Already None unless select_traceable_owner found a confident person.
        "first_name": first_name,
        "last_name": last_name,
        # Previously hard-coded to None with a comment promising they were
        # "populated below" — nothing ever did, so Tracerfy never received the
        # mailing address we already hold (Codex, 2026-09-02).
        "mail_address": (mail_parsed["street"] if mail_parsed else None),
        "mail_city": (mail_parsed["city"] if mail_parsed else None),
        "mail_state": (mail_parsed["state"] if mail_parsed else None),
        "mail_zip": (mail_parsed["zip"] if mail_parsed else None),
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
