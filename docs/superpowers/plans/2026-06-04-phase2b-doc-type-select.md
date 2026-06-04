# Phase 2b — Choose Pre-Foreclosure Doc Type: Implementation Plan

> subagent-driven, TDD, one task per commit.

**Goal:** Let users select which pre-foreclosure document type(s) a config scrapes. **`doc_types = None` preserves today's exact output (no shrink)** — selection only narrows.

**Design (Claude + Codex, sessions 584178 / 78866):**
- One **code-level capability registry** (`src/scrapers/doc_types.py`): canonical vocab + per-county/template availability (fail-closed) + canonical→scraper-token maps + normalize().
- New `scraper_configs.doc_types` JSON column (migration 036). Validation in the **route/service**, not Pydantic. Validate on create + update + defensively at job-time + in scraper.
- Plumb via **constructor param** through `_run_scraper`. Ship **King + Pierce**; EagleWeb **hidden/unsupported** until per-county coverage verified.
- `/connectors` exposes `available_doc_types` (from the registry).

**Branch:** `feature/phase2b-doc-type-select`. **Migration:** 036 (head 035). **Constraint:** no test DB/Playwright; verify via pure unit tests + offline render + Codex.

**Canonical vocab:** `notice_of_default`, `notice_of_trustee_sale`, `lis_pendens`, `notice_of_foreclosure`, `foreclosure`.

**Availability (verified):**
- King (king/wa): `[notice_of_trustee_sale]`, method `search_text`, confidence `verified`, default NOTS. canonical→search_text: `notice_of_trustee_sale → "notice of trustee sale"`.
- Pierce (pierce/wa): `[notice_of_default, notice_of_trustee_sale, lis_pendens, notice_of_foreclosure]`, method `checkbox`, confidence `verified`, default NOD. canonical→id: NOD→187, notice_of_foreclosure→188, lis_pendens→146, notice_of_trustee_sale→324.
- EagleWeb template (kitsap + others): `[notice_of_default, notice_of_trustee_sale, lis_pendens, foreclosure]`, method `keyword`, confidence `keyword`, **`supported_for_selection=False` (hidden)** until per-county coverage verified.
- Snohomish: not listed (inactive) → fail-closed = no selection offered.

---

## Task 1: Capability registry + normalization (pure, fully tested)

**Files:** `src/scrapers/doc_types.py` (new), `tests/test_doc_types_registry.py` (new)

- [ ] **Step 1** — Write `tests/test_doc_types_registry.py`:
```python
"""Phase 2b: canonical doc-type registry — pure, no DB/scraper."""
import pytest
from src.scrapers.doc_types import (
    CANONICAL_DOC_TYPES,
    availability_for,
    canonical_tokens_for,
    normalize_doc_type,
    validate_selection,
)


def test_canonical_vocab():
    assert set(CANONICAL_DOC_TYPES) == {
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "notice_of_foreclosure", "foreclosure",
    }


def test_normalize_maps_raw_strings():
    assert normalize_doc_type("NOTICE OF TRUSTEE SALE") == "notice_of_trustee_sale"
    assert normalize_doc_type("Trustee's Sale") == "notice_of_trustee_sale"
    assert normalize_doc_type("NOTICE OF DEFAULT") == "notice_of_default"
    assert normalize_doc_type("LIS PENDENS") == "lis_pendens"
    assert normalize_doc_type("totally unknown doc") is None


def test_availability_king_is_nots_only():
    a = availability_for("king", "wa")
    assert a is not None
    assert a["available"] == ["notice_of_trustee_sale"]
    assert a["default"] == "notice_of_trustee_sale"
    assert a["confidence"] == "verified"


def test_availability_pierce_has_four_default_nod():
    a = availability_for("pierce", "wa")
    assert set(a["available"]) == {
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "notice_of_foreclosure",
    }
    assert a["default"] == "notice_of_default"


def test_unknown_county_fails_closed():
    assert availability_for("nowhere", "zz") is None


def test_validate_selection_rejects_unavailable_for_county():
    ok, err = validate_selection("king", "wa", ["notice_of_default"])
    assert ok is False and "notice_of_default" in err
    ok, err = validate_selection("king", "wa", ["notice_of_trustee_sale"])
    assert ok is True and err is None
    ok, err = validate_selection("king", "wa", [])
    assert ok is False  # empty selection is invalid (use None to mean "all/legacy")


def test_canonical_tokens_pierce_maps_to_checkbox_ids():
    toks = canonical_tokens_for("pierce", "wa", ["notice_of_default", "lis_pendens"])
    assert set(toks) == {"187", "146"}


def test_canonical_tokens_king_maps_to_search_text():
    toks = canonical_tokens_for("king", "wa", ["notice_of_trustee_sale"])
    assert toks == ["notice of trustee sale"]
```

- [ ] **Step 2** — Run `python -m pytest tests/test_doc_types_registry.py -q` → fails (module missing).

- [ ] **Step 3** — Implement `src/scrapers/doc_types.py`:
```python
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
```

- [ ] **Step 4** — Run `python -m pytest tests/test_doc_types_registry.py -q` → all pass. `python -m py_compile src/scrapers/doc_types.py`. `ruff check`.

- [ ] **Step 5** — Commit:
```
git add src/scrapers/doc_types.py tests/test_doc_types_registry.py
git commit -m "feat(doc-type): canonical capability registry + normalization (Phase 2b)

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 2: `scraper_configs.doc_types` column (migration 036) + model
(model column JSON nullable; migration additive nullable; offline-render verified — same pattern as 035.)

## Task 3: Route/service validation (create + update)
Validate `doc_types` via the registry in the route handler (NOT Pydantic). `None` allowed (legacy). Non-empty must pass `validate_selection`. Reject for non-pre_foreclosure record types. Add to update/patch path too.

## Task 4: Plumb selection into King + Pierce (constructor param)
`_run_scraper` passes `doc_types` to scrapers whose constructor accepts it. King/Pierce constructors gain optional `doc_types`; when present (+ pre_foreclosure), map canonical→tokens via `canonical_tokens_for` and use that SUBSET instead of the hardcoded full set. When `None`, unchanged (legacy). Defensive: intersect with availability; ignore/raise on unavailable. EagleWeb left on legacy (hidden) for now.

## Task 5: `/connectors` exposes `available_doc_types`
`ConnectorResponse` gains `pre_foreclosure_doc_types` computed from the registry per county (only where `supported_for_selection`), incl. confidence + note, so the UI can render the selector.

## Task 6: Verification gate (Codex full diff + oracle) + journal/memory.

## Task 7 (frontend repo, flagged): doc-type selector UI on pre-foreclosure configs + "Doc Type" availability/confidence labels.

---
## Coverage / risk
- Biggest risk (Codex): silent behavior shrink from ambiguous `null` → mitigated by "null = legacy, explicit-only narrows, `[]`=422".
- Second: registry drift between availability and token maps → the registry is one module; a coverage test asserts every active pre_foreclosure connector is supported-or-hidden (add in Task 5/6).
