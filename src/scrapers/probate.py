"""Shared PROBATE party-orientation helpers.

SINGLE SOURCE OF TRUTH for putting the DECEDENT (not a filing agency, issuing
state, court, or "Estate of" caption) into ``party_name`` on probate records.

Why this exists: on a Certificate of Death the county recorder routinely indexes
the *issuing authority* — "STATE OF WASHINGTON", "WASHINGTON STATE DEPARTMENT OF
HEALTH" — as the grantor, with the actual DECEDENT in the grantee slot. A scraper
that copies ``party_name = grantor`` verbatim then surfaces the agency as the lead.
Live data (cowlitz 12/42, king 1/65, okanogan 1/23) confirmed this across the
county scrapers that had not yet adopted the per-template fixes first shipped for
EagleWeb (``_strip_filing_agency``) and Skagit (``_is_filing_state_party``). This
module consolidates that proven logic so every probate scraper shares one rule.

PROBATE ONLY — never call from pre_foreclosure / tax / divorce / code_violation
paths. The transforms are no-ops when the grantor is already a real person, so
applying this to a county whose grantor IS the decedent does not change output.

Design guards (pressure-tested with Codex):
  1. Promote the grantee to ``party_name`` ONLY when the grantor was wholly a
     filing agency/state AND the grantee is person-like (not itself an agency/org).
  2. If both grantor and grantee are agency/empty, return (None, None) — never let
     a raw agency name reach a lead.
  3. No-op the agency swap on TRANSFER ON DEATH deeds: the grantor there is a
     LIVING owner/transferor, so swapping to the grantee would corrupt a correct
     party. (Also structurally safe — a living owner never matches the agency
     regexes — but the doc_type guard makes the intent explicit.)
"""
from __future__ import annotations

import re

# --- Filing-agency / issuing-authority detection -------------------------------

# WA State Department of Health — the Certificate-of-Death issuing authority, not
# the decedent. The optional "STATE OF WA[SHINGTON]," qualifier covers the shape
# where the recorder concatenates the full agency name. No legitimate grantor is
# "...DEPARTMENT OF HEALTH", so the phrase is stripped wherever it appears.
_HEALTH_DEPT_RE = re.compile(
    r"(?:(?:STATE\s+OF\s+)?WA(?:SH(?:INGTON)?)?\.?\s*,?\s*)?"
    r"(?:DEPARTMENT|DEPT)\.?\s+OF\s+HEALTH\b",
    re.IGNORECASE,
)

# A lone state name/abbreviation left as residue after the agency phrase is
# stripped ("WA DEPT OF HEALTH" -> "WA"). Treated as empty so the caller falls
# back to the grantee instead of surfacing "WA" as the party.
_LONE_STATE_RE = re.compile(r"^\s*(?:WA|WASH|WN|WASHINGTON)\.?\s*$", re.IGNORECASE)

# A WHOLE value that is a bare filing state, in either order:
#   "STATE OF WASHINGTON", "WASHINGTON STATE", "WASH. STATE OF", "STATE OF WA".
# Anchored ^...$ and matched per " / "-split segment so it never fires on a real
# entity that merely starts with a state name ("WASHINGTON STATE UNIVERSITY") or
# contains the substring ("INTERSTATE ..." — guarded by \bSTATE). State-agnostic
# so an out-of-state death certificate is corrected too.
_BARE_STATE_RE = re.compile(
    r"^\s*(?:"
    r"STATE\s+OF\s+[A-Z][A-Z.\s]*"          # STATE OF <state>
    r"|[A-Z][A-Z.\s]*\bSTATE(?:\s+OF)?"      # <state> STATE [OF]
    r")\s*$",
    re.IGNORECASE,
)

# Tokens that are status words only (no real name). A grantor that reduces to
# only these after agency-strip is treated as empty so the caller falls back.
_STATUS_ONLY_TOKENS = {"DECEASED", "DECEDENT", "DECD", "ESTATE", "OF", "THE"}

# Non-person markers that disqualify a value from being a probate party. Used to
# guard the grantee before promoting it (Codex guard #1) — an agency, court, or
# company must never become the lead.
_NON_PERSON_RE = re.compile(
    r"\bDEPARTMENT\b|\bDEPT\b|\bHEALTH\b|\bAUDITOR\b|\bRECORDER\b|\bASSESSOR\b"
    r"|\b(?:SUPERIOR|DISTRICT)\s+COURT\b|\bCOUNTY\s+CLERK\b|\bCLERK\s+OF\b"
    r"|\bVITAL\s+(?:RECORDS|STATISTICS)\b"
    r"|\b(?:LLC|L\.?L\.?C|INC|CORP|CORPORATION|COMPANY|BANK|ESCROW|ASSOCIATION)\b"
    r"|\bTITLE\s+CO\b",
    re.IGNORECASE,
)

# Leading "Estate of" caption to strip down to the decedent name.
_ESTATE_CAPTION_RE = re.compile(
    r"^\s*(?:IN\s+RE(?:\s+THE)?\s+)?(?:THE\s+)?ESTATE\s+OF\s+",
    re.IGNORECASE,
)

# Doc types whose grantor is a LIVING owner — the agency swap must NOT fire.
_LIVING_OWNER_DOC_RE = re.compile(r"TRANSFER\s+ON\s+DEATH|\bTOD\b", re.IGNORECASE)


def strip_filing_agency(name: str | None) -> str:
    """Remove the Certificate-of-Death filing agency from a grantor name.

    "PERRIN, RONALD, STATE OF WA, DEPT OF HEALTH" -> "PERRIN, RONALD".
    "STATE OF WASHINGTON DEPARTMENT OF HEALTH"    -> "" (agency only).
    "STATE OF WASHINGTON"                          -> "" (bare state only).
    "WASHINGTON STATE UNIVERSITY"                  -> unchanged (not the filer).

    Returns the cleaned name, or "" when the value was wholly the agency/state
    (the caller then falls back to the grantee).
    """
    if not name:
        return name or ""
    cleaned = _HEALTH_DEPT_RE.sub("", name)
    cleaned = re.sub(r"(?:\s*/\s*){2,}", " / ", cleaned)   # collapse left-behind separators
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" ,/").strip()
    if not cleaned:
        return ""
    # A lone state name/abbrev left behind ("WA DEPT OF HEALTH" -> "WA").
    if _LONE_STATE_RE.match(cleaned):
        return ""
    # Every " / "-split segment is a bare filing state -> nothing real remains.
    parts = [p for p in cleaned.split(" / ") if p.strip()]
    if parts and all(_BARE_STATE_RE.match(p) for p in parts):
        return ""
    tokens = set(re.findall(r"[A-Z]+", cleaned.upper()))
    if tokens and tokens <= _STATUS_ONLY_TOKENS:
        return ""
    return cleaned


def is_filing_agency_party(value: str | None) -> bool:
    """True if ``value`` is wholly a filing agency / issuing state (no real person)."""
    if not value or not value.strip():
        return False
    return strip_filing_agency(value) == ""


def is_person_like_party(value: str | None) -> bool:
    """True if ``value`` looks like a real person/decedent (not an agency/court/org)."""
    if not value or not value.strip():
        return False
    return not is_filing_agency_party(value) and not _NON_PERSON_RE.search(value.upper())


def strip_estate_caption(name: str | None) -> str | None:
    """Collapse an "ESTATE OF X / X / Y" caption to the decedent "X".

    Takes the leading " / "-segment, removes a leading "(In re) (the) Estate of"
    caption, and returns the decedent name. Falls back to the next non-caption
    segment if the first reduces to empty.

    Returns the input UNCHANGED when no caption is present — so a legitimately
    stacked multi-grantor party ("SMITH JOHN / SMITH JANE") keeps every co-party.
    The collapse-to-first-segment only happens for an actual caption (where the
    later segments are alias/heir noise, e.g. okanogan's
    "ESTATE OF GLENNA K JONES / JONES, GLENNA K / JONES, GLENN I").
    """
    if not name:
        return name
    if not _ESTATE_CAPTION_RE.search(name):
        return name  # no caption -> leave stacked co-parties intact
    segments = [s.strip() for s in name.split(" / ") if s.strip()]
    if not segments:
        return name
    decedent = _ESTATE_CAPTION_RE.sub("", segments[0]).strip(" ,/").strip()
    if decedent:
        return decedent
    for seg in segments[1:]:
        if not _ESTATE_CAPTION_RE.match(seg):
            return seg
    return name


def orient_probate_party(
    grantor: str | None,
    grantee: str | None,
    doc_type: str | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(party_name, heirs)`` with the DECEDENT as ``party_name``.

    - Strips the filing agency/state from the grantor.
    - If the grantor was wholly an agency, promotes a person-like grantee
      (guard #1); if both are agency/empty -> (None, None) (guard #2).
    - No-ops the agency swap on Transfer-on-Death deeds (guard #3): the grantor
      is a living owner.
    - Strips a leading "Estate of" caption from the resulting party.

    A no-op for the common case where the grantor is already the decedent.
    """
    g = (grantor or "").strip()
    e = (grantee or "").strip()

    # Guard #3 — TOD deed: grantor is a living owner; never swap. Caption-strip only.
    if doc_type and _LIVING_OWNER_DOC_RE.search(doc_type):
        return (strip_estate_caption(g) or None, e or None)

    deagencied = strip_filing_agency(g)
    if deagencied != g:
        # An agency phrase was present in the grantor.
        if deagencied:
            party, heirs = deagencied, (e or None)
        elif is_person_like_party(e):          # guard #1
            party, heirs = e, None
        else:                                   # guard #2 — both agency/empty
            party, heirs = None, None
    else:
        party, heirs = (g or None), (e or None)

    return (strip_estate_caption(party), heirs)
