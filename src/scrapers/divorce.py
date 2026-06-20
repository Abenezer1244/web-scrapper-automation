"""Shared DIVORCE classification + party-orientation helpers.

SINGLE SOURCE OF TRUTH for deciding whether a recorded document is a marital
divorce/dissolution lead, and for keeping a real PERSON (not a court, state, or
agency) in ``party_name``.

Why this exists
---------------
Every divorce-capable scraper previously filtered with its own ad-hoc keyword
list (``["DIVORCE", "DISSOLUTION", "DECREE OF DISSOLUTION"]``) using naive
substring matching. Two failure modes followed:

1. Bare ``DISSOLUTION`` matches CORPORATE / LLC / partnership dissolution
   ("ARTICLES OF DISSOLUTION", "CERTIFICATE OF DISSOLUTION", "LLC DISSOLUTION").
   Those are recorded business documents — never a motivated-seller divorce lead —
   so they silently polluted the divorce channel.
2. Skagit additionally matched bare ``SEPARATION`` (property-line / business /
   utility separations), another non-divorce source.

The hard product reality (pressure-tested with Codex): a divorce decree is
overwhelmingly a *Superior Court* record, not a county *recorder/auditor* record.
King's divorce connector is already an inactive placeholder for exactly this
reason. So the safe default is to FAIL CLOSED on an ambiguous bare ``DISSOLUTION``
unless the connector has a precise server-side divorce filter (Pierce ARMS
checkbox 87 = "DECREE OF DISSOLUTION"; Skagit server doc-type "Decree-divorce").

Classification is therefore THREE-STATE, not boolean:
  - MATCH      — unambiguous marital divorce / dissolution-of-marriage / legal
                 separation. Always a divorce lead.
  - NON_MATCH  — explicitly non-marital (corporate/entity dissolution, bare
                 separation, or no divorce signal at all). Never a divorce lead.
  - AMBIGUOUS  — a bare "DISSOLUTION" / "DECREE" with no entity and no marital
                 qualifier. A divorce lead ONLY when the caller passes
                 ``precise_source=True`` (the server already guaranteed divorce).

Party orientation (``orient_divorce_party``) is deliberately NARROW. Both spouses
on a dissolution are valid leads, so there is nothing to "reorder" in the common
case — the only correction is promoting a real person when the recorder indexed a
court / state / agency (e.g. "STATE OF WASHINGTON" in a DSHS child-support
dissolution) into the lead slot. It reuses ``probate.is_person_like_party`` for the
non-person test and is a no-op whenever ``grantor`` is already a person.

NOTE on ``heirs``: for a non-probate record ``heirs`` is just the SECONDARY party
(the other spouse). The shared schema reuses the column; this module does not try
to make the name accurate, only to keep a real person in ``party_name``.

DIVORCE ONLY — never call from probate / pre_foreclosure / tax / code_violation.
"""
from __future__ import annotations

import re
from enum import Enum

from src.scrapers.probate import is_person_like_party


class DivorceMatch(Enum):
    """Three-state classification of a recorder document type for divorce."""

    MATCH = "match"
    NON_MATCH = "non_match"
    AMBIGUOUS = "ambiguous"


# --- Unambiguous marital positives --------------------------------------------
# Each phrase is, on its own, a marital divorce / dissolution / legal-separation
# decree. "DECREE OF DISSOLUTION" is safe as a strong positive: a *court decree*
# of dissolution is the WA family-law divorce instrument, whereas a *business*
# dissolution is recorded as "ARTICLES/CERTIFICATE/STATEMENT OF DISSOLUTION"
# (handled by the corporate negatives below), never as a "decree". WA registered
# domestic-partnership dissolution is family law, so it is a positive too — and is
# matched BEFORE the bare-"PARTNERSHIP" corporate negative can fire.
_STRONG_POSITIVE: tuple[str, ...] = (
    "DISSOLUTION OF MARRIAGE",
    "DISSOLUTION OF DOMESTIC PARTNERSHIP",
    "DECREE OF DISSOLUTION",
    "DECREE OF LEGAL SEPARATION",
    "LEGAL SEPARATION",
    "MARRIAGE DISSOLUTION",
    "DIVORCE",
)

# --- Explicit non-marital negatives -------------------------------------------
# Business / entity dissolution document types and the bare-separation variants
# the user chose to exclude. Checked AFTER the strong positives so
# "DISSOLUTION OF DOMESTIC PARTNERSHIP" (a positive) is not clipped by the
# "PARTNERSHIP" / "DISSOLUTION OF PARTNERSHIP" entries here.
_NEGATIVE_PHRASES: tuple[str, ...] = (
    "ARTICLES OF DISSOLUTION",
    "CERTIFICATE OF DISSOLUTION",
    "STATEMENT OF DISSOLUTION",
    "NOTICE OF DISSOLUTION",
    "DISSOLUTION OF PARTNERSHIP",
    "DISSOLUTION OF LIMITED",
    "DISSOLUTION OF CORPORATION",
    "DISSOLUTION OF NONPROFIT",
    "DISSOLUTION OF NON-PROFIT",
    "PROPERTY SETTLEMENT",     # "PROPERTY SETTLEMENT AGREEMENT" — not the decree
    "SEPARATION AGREEMENT",
)

# Whole-word entity tokens. If any appears in the doc type it is a corporate /
# entity dissolution, not a divorce. Word-boundary matched so "CORP" does not fire
# inside "CORPORAL" and "LP" does not fire inside "HELP".
_ENTITY_TOKENS: tuple[str, ...] = (
    "LLC", "LLP", "LP", "PLLC", "INC", "CORP", "CORPORATION",
    "COMPANY", "PARTNERSHIP", "NONPROFIT", "BUSINESS", "ENTITY",
)
_ENTITY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _ENTITY_TOKENS) + r")\b",
    re.IGNORECASE,
)

# Bare ambiguous markers — a "DISSOLUTION" or "DECREE" with no marital qualifier
# and no entity token. Matched whole-word so they do not fire inside a longer word.
_AMBIGUOUS_RE = re.compile(r"\b(?:DISSOLUTION|DECREE)\b", re.IGNORECASE)


def classify_divorce_doc(
    doc_type: str | None, *, precise_source: bool = False
) -> DivorceMatch:
    """Classify a recorder document-type string for the divorce record type.

    Order matters:
      1. Unambiguous marital positive  -> MATCH.
      2. Explicit corporate/entity dissolution or excluded separation -> NON_MATCH.
      3. Bare "DISSOLUTION"/"DECREE" with no qualifier -> AMBIGUOUS.
      4. Anything else (no divorce signal) -> NON_MATCH.

    ``precise_source`` does not change the classification — it changes how the
    AMBIGUOUS bucket is *resolved* by ``is_divorce_doc``. It is accepted here so a
    caller can branch on the raw three-state result if needed.
    """
    if not doc_type or not doc_type.strip():
        return DivorceMatch.NON_MATCH

    up = doc_type.upper()

    if any(p in up for p in _STRONG_POSITIVE):
        return DivorceMatch.MATCH

    if any(p in up for p in _NEGATIVE_PHRASES) or _ENTITY_RE.search(up):
        return DivorceMatch.NON_MATCH

    if _AMBIGUOUS_RE.search(up):
        return DivorceMatch.AMBIGUOUS

    return DivorceMatch.NON_MATCH


def is_divorce_doc(doc_type: str | None, *, precise_source: bool = False) -> bool:
    """True if ``doc_type`` should be kept as a divorce lead.

    MATCH is always kept. AMBIGUOUS (bare "DISSOLUTION"/"DECREE") is kept ONLY
    when ``precise_source=True`` — i.e. the connector already constrained the
    server-side search to divorce (Pierce checkbox, Skagit doc-type), so a
    leftover bare label is trustworthy. Generic keyword-only template connectors
    pass ``precise_source=False`` and therefore drop the ambiguous bucket
    (fail-closed), keeping corporate dissolutions out of divorce results.
    """
    result = classify_divorce_doc(doc_type, precise_source=precise_source)
    if result is DivorceMatch.MATCH:
        return True
    if result is DivorceMatch.AMBIGUOUS:
        return precise_source
    return False


def orient_divorce_party(
    grantor: str | None,
    grantee: str | None,
    doc_type: str | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(party_name, heirs)`` keeping a real PERSON as ``party_name``.

    Narrow by design: both spouses on a dissolution are valid leads, so this only
    corrects the case where the recorder indexed a non-person (court / state /
    agency / company) into the grantor slot while the grantee is a real person —
    then the person is promoted. A no-op whenever the grantor is already a person
    (the overwhelming common case) so it cannot corrupt correct rows.

    ``heirs`` here is the SECONDARY party (the other spouse), reusing the shared
    schema column; this function does not relabel it.
    """
    g = (grantor or "").strip()
    e = (grantee or "").strip()

    # Promote the grantee ONLY when the grantor is clearly a non-person AND the
    # grantee is clearly a person. Drop the non-person grantor rather than parking
    # an agency/court name in the secondary slot.
    if g and not is_person_like_party(g) and is_person_like_party(e):
        return (e or None, None)

    return (g or None, e or None)
