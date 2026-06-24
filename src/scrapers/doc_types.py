"""Canonical pre-foreclosure document-type registry (Phase 2b).

SINGLE SOURCE OF TRUTH for: the canonical doc-type vocabulary, which types each
county/template actually exposes (fail-closed), how a raw scraped doc string
normalizes to canonical, and how a canonical selection maps to each scraper's
own tokens (King search-text, Pierce checkbox id, EagleWeb keyword).

`doc_types = None` on a config means LEGACY behavior (today's output) — this
registry is consulted ONLY when an explicit selection is present. Do not use it
to "default" a null selection; that would silently shrink existing lists.
"""
from __future__ import annotations

CANONICAL_DOC_TYPES = [
    "notice_of_default",
    "notice_of_trustee_sale",
    "lis_pendens",
    "notice_of_foreclosure",
    "foreclosure",
]

# Human-readable label per canonical token — the display text the frontend
# doc-type selector shows next to each checkbox.
CANONICAL_DOC_TYPE_LABELS = {
    "notice_of_default": "Notice of Default",
    "notice_of_trustee_sale": "Notice of Trustee Sale",
    "lis_pendens": "Lis Pendens",
    "notice_of_foreclosure": "Notice of Foreclosure",
    "foreclosure": "Foreclosure",
}

# Raw-string -> canonical. Longest/most-specific patterns first.
_NORMALIZE = [
    ("notice of trustee", "notice_of_trustee_sale"),
    ("trustee's sale", "notice_of_trustee_sale"),
    ("trustee sale", "notice_of_trustee_sale"),
    ("nts", "notice_of_trustee_sale"),
    ("notice of default", "notice_of_default"),
    ("nod", "notice_of_default"),
    ("lis pendens", "lis_pendens"),
    ("lisp", "lis_pendens"),
    ("notice of foreclosure", "notice_of_foreclosure"),
    ("foreclosure", "foreclosure"),
]


def normalize_doc_type(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    for needle, canon in _NORMALIZE:
        if needle in s:
            return canon
    return None


# Per (county,state) availability. EagleWeb counties share a template default;
# add explicit overrides as coverage is verified. supported_for_selection=False
# hides a county from the UI selector (registry still defines it for clarity).
_AVAILABILITY: dict[tuple[str, str], dict] = {
    ("king", "wa"): {
        "available": ["notice_of_trustee_sale"],
        "method": "search_text",
        "confidence": "verified",
        "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {"notice_of_trustee_sale": "notice of trustee sale"},
        "note": "King recorder exposes NOTS; NOD is usually not recorded in WA non-judicial foreclosure.",
    },
    ("pierce", "wa"): {
        "available": [
            "notice_of_default", "notice_of_trustee_sale",
            "lis_pendens", "notice_of_foreclosure",
        ],
        "method": "checkbox",
        "confidence": "verified",
        "default": "notice_of_default",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_default": "187",
            "notice_of_foreclosure": "188",
            "lis_pendens": "146",
            "notice_of_trustee_sale": "324",
        },
    },
    # Clark (LandmarkWeb) — exact server-side document-type checkboxes. The portal
    # filters server-side to the selected codes (verified live 2026-06-22, codes in
    # clark_wa._DOC_TYPE_CHECKBOX_VALUES / _CHECKBOX_DOC_LABELS). notice_of_trustee_sale
    # maps to BOTH 167 (NOTICE OF TRUSTEE SALE) and 257 (TRUSTEES SALE) variants.
    ("clark", "wa"): {
        "available": [
            "notice_of_default", "notice_of_trustee_sale",
            "lis_pendens", "notice_of_foreclosure", "foreclosure",
        ],
        "method": "checkbox",
        "confidence": "verified",
        "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_default": "166",
            "notice_of_trustee_sale": ["167", "257"],
            "lis_pendens": "129",
            "notice_of_foreclosure": "157",
            "foreclosure": "93",
        },
        "note": "Clark recorder LandmarkWeb — exact server-side document-type checkboxes (verified live 2026-06-22).",
    },
    # Skagit (custom ASP.NET recorder) — server-side document-type DROPDOWN
    # (content_ddlDocumentType) selected per search, then a client-side comment
    # refine. tokens map canonical -> the EXACT dropdown option label. The dropdown
    # has no generic "Foreclosure" option, so `foreclosure` is not offered (the four
    # specific notice types are). The scraper additionally narrows its client-refine
    # keyword set via its own canonical->keyword map (both stages narrow together).
    ("skagit", "wa"): {
        "available": [
            "notice_of_default", "notice_of_trustee_sale",
            "lis_pendens", "notice_of_foreclosure",
        ],
        "method": "dropdown",
        "confidence": "verified",
        "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_default": "Notice Of Default",
            "notice_of_trustee_sale": "Notice Of Trustees Sale",
            "lis_pendens": "Lis Pendens",
            "notice_of_foreclosure": "Notice Of Foreclosure",
        },
        "note": "Skagit recorder document-type dropdown (server-side), refined client-side by comment.",
    },
    # ── P4: client-side KEYWORD families (confidence="keyword"). Each tokens map
    # EXACTLY partitions that scraper's _DOC_TYPE_MAP["pre_foreclosure"] so a narrowed
    # selection is a true subset (enforced by test_*_tokens_partition_scraper_map).
    # "NOTICE OF INTENT TO FORFEIT" (real-estate-contract forfeiture, iDoc/Tyler) is
    # grouped under `foreclosure` (generic foreclosure-stage), the closest canonical.
    # Douglas — AcclaimWeb (chelan, the other Acclaim county, is health=down/deferred).
    ("douglas", "wa"): {
        "available": ["notice_of_default", "notice_of_trustee_sale", "lis_pendens", "foreclosure"],
        "method": "keyword", "confidence": "keyword", "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_default": ["NOTICE OF DEFAULT", "NOD"],
            "notice_of_trustee_sale": ["NOTICE OF TRUSTEE", "TRUSTEE SALE", "TRUSTEE'S SALE", "NTS"],
            "lis_pendens": ["LIS PENDENS"],
            "foreclosure": ["FORECLOSURE"],
        },
        "note": "AcclaimWeb recorder — best-effort document-type text match (client-side).",
    },
    # Columbia — iDocMarket.
    ("columbia", "wa"): {
        "available": ["notice_of_default", "notice_of_trustee_sale", "lis_pendens", "foreclosure"],
        "method": "keyword", "confidence": "keyword", "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_trustee_sale": ["NOTICE OF TRUSTEE", "TRUSTEE'S SALE", "TRUSTEE SALE"],
            "notice_of_default": ["NOTICE OF DEFAULT"],
            "lis_pendens": ["LIS PENDENS"],
            "foreclosure": ["FORECLOSURE", "NOTICE OF INTENT TO FORFEIT"],
        },
        "note": "iDocMarket recorder — best-effort document-type text match (client-side).",
    },
    # Cowlitz — Laserfiche WebLink.
    ("cowlitz", "wa"): {
        "available": ["notice_of_default", "notice_of_trustee_sale", "lis_pendens", "foreclosure"],
        "method": "keyword", "confidence": "keyword", "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "lis_pendens": ["LIS PENDENS"],
            "notice_of_trustee_sale": ["NOTICE OF TRUSTEE", "TRUSTEE SALE", "TRUSTEE'S SALE"],
            "notice_of_default": ["NOTICE OF DEFAULT"],
            "foreclosure": ["FORECLOSURE"],
        },
        "note": "Laserfiche WebLink recorder — best-effort document-type text match (client-side).",
    },
    # Okanogan — Tyler SelfService.
    ("okanogan", "wa"): {
        "available": ["notice_of_default", "notice_of_trustee_sale", "lis_pendens", "foreclosure"],
        "method": "keyword", "confidence": "keyword", "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "lis_pendens": ["LIS PENDENS"],
            "notice_of_trustee_sale": ["NOTICE OF TRUSTEE", "TRUSTEE'S SALE"],
            "notice_of_default": ["NOTICE OF DEFAULT"],
            "foreclosure": ["FORECLOSURE", "NOTICE OF INTENT TO FORFEIT"],
        },
        "note": "Tyler SelfService recorder — best-effort document-type text match (client-side).",
    },
    # Whatcom — Helion (manual). The only keyword family that exposes all 5 canonical
    # types (its map has a distinct NOTICE OF FORECLOSURE token).
    ("whatcom", "wa"): {
        "available": [
            "notice_of_default", "notice_of_trustee_sale", "lis_pendens",
            "notice_of_foreclosure", "foreclosure",
        ],
        "method": "keyword", "confidence": "keyword", "default": "notice_of_trustee_sale",
        "supported_for_selection": True,
        "tokens": {
            "notice_of_trustee_sale": ["NOTICE OF TRUSTEE SALE"],
            "lis_pendens": ["LIS PENDENS"],
            "notice_of_default": ["NOTICE OF DEFAULT"],
            "notice_of_foreclosure": ["NOTICE OF FORECLOSURE"],
            "foreclosure": ["FORECLOSURE"],
        },
        "note": "Whatcom Helion recorder — best-effort document-type text match (client-side).",
    },
}

# EagleWeb template (Tyler EagleWeb recorder sites). CLIENT-SIDE keyword filter:
# the scraper fetches broadly and keeps rows whose doc-type text matches the keyword
# set, so selection NARROWS that keyword set — a weaker, best-effort filter than a
# server-side checkbox/dropdown, hence confidence="keyword" (the UI labels it as a
# post-collection text match, not a portal filter).
#
# `tokens` MUST exactly partition eagleweb._DOC_TYPE_MAP["pre_foreclosure"] so a
# narrowed selection is a true SUBSET of legacy output — never matching a keyword the
# full run wouldn't (would over-collect) and never dropping one it would (silent lead
# loss). test_eagleweb_tokens_partition_scraper_map enforces this lockstep.
# notice_of_foreclosure is intentionally absent: EagleWeb's keyword map has no
# "NOTICE OF FORECLOSURE" token (only bare FORECLOSURE → canonical `foreclosure`).
_EAGLEWEB_TEMPLATE = {
    "available": [
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "foreclosure",
    ],
    "method": "keyword",
    "confidence": "keyword",
    "default": "notice_of_trustee_sale",
    "supported_for_selection": True,
    "tokens": {
        "notice_of_default": ["NOTICE OF DEFAULT"],
        "notice_of_trustee_sale": ["NOTICE OF TRUSTEE SALE", "TRUSTEE SALE", "TRUSTEE'S SALE", "NTS", "NTSCL"],
        "lis_pendens": ["LIS PENDENS", "LISP"],
        "foreclosure": ["FORECLOSURE"],
    },
}
# Healthy EagleWeb pre_foreclosure counties (live `/connectors` 2026-06-23). The 4
# health=down EagleWeb counties (lewis, pacific, spokane) are deferred fail-closed
# until their portals can be live-checked. grant sits on tylerhost.net but its
# /grantrecorder/web/ path resolves to EagleWeb (see registry._detect_template).
_EAGLEWEB_COUNTIES = {
    "benton", "clallam", "grant", "island",
    "jefferson", "kitsap", "thurston", "whitman",
}


def availability_for(county: str, state: str) -> dict | None:
    """Return the availability dict for a county, or None (fail-closed) if the
    county is not explicitly known to support pre-foreclosure doc-type selection."""
    key = (county.strip().lower(), state.strip().lower())
    if key in _AVAILABILITY:
        return _AVAILABILITY[key]
    if key[0] in _EAGLEWEB_COUNTIES and key[1] == "wa":
        return _EAGLEWEB_TEMPLATE
    return None


def validate_selection(county: str, state: str, doc_types: list[str]) -> tuple[bool, str | None]:
    """True if every selected canonical type is available for this county.
    An empty list is INVALID (None means legacy/all — empty means nothing)."""
    a = availability_for(county, state)
    if a is None or not a.get("supported_for_selection"):
        return False, f"{county}, {state} does not support pre-foreclosure document-type selection"
    if not doc_types:
        return False, "doc_types must contain at least one document type (omit the field for default behavior)"
    bad = [d for d in doc_types if d not in a["available"]]
    if bad:
        return False, f"document type(s) not available for {county}, {state}: {bad}"
    return True, None


def selectable_availability(county: str, state: str) -> dict | None:
    """UI-facing availability for the pre-foreclosure doc-type selector, or None
    when the county does not support selection (fail-closed / hidden). Excludes
    internal token maps; returns only what the frontend needs to render options."""
    a = availability_for(county, state)
    if a is None or not a.get("supported_for_selection"):
        return None
    return {
        "available": a["available"],
        "default": a.get("default"),
        "confidence": a.get("confidence"),
        "method": a.get("method"),
        "note": a.get("note"),
    }


def selectable_doc_type_labels(county: str, state: str) -> dict[str, str] | None:
    """{canonical_token: human_label} for the pre-foreclosure doc types a county
    exposes for selection, or None if it doesn't support selection.

    This is the EXACT shape the frontend doc-type checkbox selector consumes
    (it renders Object.entries() as token->label). Do not return the richer
    `selectable_availability` metadata object here — the UI iterates the dict as
    a token->label map, so metadata keys would render as bogus checkboxes.
    """
    a = availability_for(county, state)
    if a is None or not a.get("supported_for_selection"):
        return None
    return {
        t: CANONICAL_DOC_TYPE_LABELS.get(t, t.replace("_", " ").title())
        for t in a["available"]
    }


def canonical_tokens_for(county: str, state: str, doc_types: list[str]) -> list:
    """Map a validated canonical selection to the scraper's own tokens
    (King search-text str, Pierce checkbox id str, EagleWeb keyword lists)."""
    a = availability_for(county, state)
    if a is None:
        return []
    tokens = a["tokens"]
    out: list = []
    for d in doc_types:
        tok = tokens.get(d)
        if tok is None:
            # All-or-nothing: a stale/unmapped type means we cannot trust the
            # narrowed set, so return [] → the scraper falls back to its full
            # legacy set (returns more, never a wrongly-shrunk list). Codex P2.
            return []
        if isinstance(tok, list):
            out.extend(tok)
        else:
            out.append(tok)
    return out


def canonical_tokens_or_raise(county: str, state: str, doc_types: list[str]) -> list:
    """Map an EXPLICIT canonical selection to the scraper's own tokens, FAIL-CLOSED.

    Use this from scraper constructors when a non-None ``doc_types`` selection is
    present. Unlike :func:`canonical_tokens_for` (which returns ``[]`` so a *legacy*
    caller can fall back to its full set), an explicit user selection must NEVER
    silently broaden: if the county is not selectable, the selection is empty, or any
    selected token is unmappable (e.g. a stale config saved before a registry change),
    raise ``ValueError`` so the job fails loud instead of scraping every document type
    the user did NOT pick. ``doc_types is None`` means legacy/full and must not reach
    this function.
    """
    a = availability_for(county, state)
    if a is None or not a.get("supported_for_selection"):
        raise ValueError(
            f"{county}, {state} does not support pre-foreclosure document-type selection"
        )
    if not doc_types:
        raise ValueError(
            "explicit doc_types selection is empty (pass None for legacy/full output)"
        )
    tokens = a["tokens"]
    out: list = []
    for d in doc_types:
        tok = tokens.get(d)
        if tok is None:
            raise ValueError(
                f"document type {d!r} is not mappable for {county}, {state} "
                f"(stale selection after a registry change?) — refusing to broaden output"
            )
        if isinstance(tok, list):
            out.extend(tok)
        else:
            out.append(tok)
    if not out:
        raise ValueError(
            f"no scraper tokens resolved for {county}, {state} selection {doc_types}"
        )
    return out
