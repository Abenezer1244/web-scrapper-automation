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
}

# EagleWeb template default (kitsap + others). Hidden from selection until
# per-county coverage is explicitly verified (Codex: don't assume 16 counties
# share one truth). Keyword map mirrors eagleweb._DOC_TYPE_MAP entries.
_EAGLEWEB_TEMPLATE = {
    "available": [
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "foreclosure",
    ],
    "method": "keyword",
    "confidence": "keyword",
    "default": None,
    "supported_for_selection": False,
    "tokens": {
        "notice_of_default": ["NOTICE OF DEFAULT", "NOD"],
        "notice_of_trustee_sale": ["NOTICE OF TRUSTEE SALE", "TRUSTEE SALE", "TRUSTEE'S SALE", "NTS"],
        "lis_pendens": ["LIS PENDENS", "LISP"],
        "foreclosure": ["FORECLOSURE"],
    },
}
_EAGLEWEB_COUNTIES = {"kitsap"}  # extend as coverage is verified


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
    if a is None:
        return False, f"{county}, {state} does not support pre-foreclosure document-type selection"
    if not doc_types:
        return False, "doc_types must contain at least one document type (omit the field for default behavior)"
    bad = [d for d in doc_types if d not in a["available"]]
    if bad:
        return False, f"document type(s) not available for {county}, {state}: {bad}"
    return True, None


def canonical_tokens_for(county: str, state: str, doc_types: list[str]) -> list:
    """Map a validated canonical selection to the scraper's own tokens
    (King search-text str, Pierce checkbox id str, EagleWeb keyword lists)."""
    a = availability_for(county, state)
    if a is None:
        return []
    out: list = []
    for d in doc_types:
        tok = a["tokens"].get(d)
        if tok is None:
            continue
        if isinstance(tok, list):
            out.extend(tok)
        else:
            out.append(tok)
    return out
