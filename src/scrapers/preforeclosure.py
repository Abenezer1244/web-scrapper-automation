"""Shared pre_foreclosure extraction correctness helpers.

A pre_foreclosure lead is a HOMEOWNER in active financial distress. Two
cross-template rules live here so every recorder scraper applies them
identically (instead of each template re-deriving — and getting wrong — its own):

1. ``is_cancellation_or_admin(doc_type)`` — DANGER guard. A recorded document
   whose type signals the foreclosure was CANCELLED / CURED (Discontinuance,
   Rescission, Cancellation, Withdrawal, Reconveyance, Satisfaction, Release) or
   is pure trustee ADMINISTRATION (Substitution / Appointment / Resignation of
   Trustee) is the OPPOSITE of an active-distress lead. These substring-match the
   legitimate keywords — "Discontinuance of Trustee's Sale" contains "Trustee's
   Sale", "Rescission of Notice of Default" contains "Notice of Default" — so a
   positive doc-type keyword hit must be REJECTED when this returns True.

2. ``orient_pre_foreclosure_party(grantor, grantee)`` — borrower orientation. On a
   Notice of Trustee's Sale the recorder frequently indexes the TRUSTEE company as
   grantor and the borrower as grantee (the inverse of a normal deed), and some
   counties stack the borrower and the trustee company together in one party cell
   ("BOYLE DAVID E / QUALITY LOAN SERVICE CORP"). Pick the real PERSON name(s) as
   party_name regardless of side; if neither side carries a person there is no
   homeowner lead.

Mirrors the person/company heuristic already proven in acclaimweb.py /
idocmarket.py; centralised here so laserfiche, ava_fidlar, tyler_selfservice,
skagit, eagleweb, landmarkweb and the King/Clark scrapers all behave the same.
"""
from __future__ import annotations

import re

# Cancellation / cured / trustee-admin substrings — checked against an
# UPPER-CASED doc-type string. Each marks a document that is NOT active distress.
# Tokens are kept SCOPED (not bare verbs) so they cannot reject a real filing
# whose type merely contains the word — e.g. bare "APPOINTMENT OF" would wrongly
# drop "Appointment of Receiver" in an active judicial foreclosure, and bare
# "RELEASE OF" could clip a mixed type (Codex review).
_CANCELLATION_ADMIN: tuple[str, ...] = (
    "DISCONTINU",                  # DISCONTINUANCE OF TRUSTEE'S SALE / OF FORECLOSURE
    "RESCISSION",
    "RESCIND",
    "CANCEL",                      # CANCELLATION OF NOTICE OF DEFAULT / OF SALE
    "WITHDRAW",                    # WITHDRAWAL OF NOTICE OF TRUSTEE'S SALE
    "RECONVEY",                    # (DEED OF) RECONVEYANCE — loan paid off
    "SATISFACTION",                # satisfaction of mortgage / judgment
    "RELEASE OF LIS PENDENS",
    "RELEASE OF MORTGAGE",
    "RELEASE OF DEED OF TRUST",
    "RELEASE OF NOTICE",           # RELEASE OF NOTICE OF DEFAULT / OF TRUSTEE'S SALE
    "SUBSTITUTION OF TRUSTEE",
    "SUBSTITUTE TRUSTEE",          # APPOINTMENT OF SUBSTITUTE TRUSTEE
    "SUCCESSOR TRUSTEE",           # APPOINTMENT OF SUCCESSOR TRUSTEE
    "RESIGNATION OF TRUSTEE",
)


def is_cancellation_or_admin(doc_type: str | None) -> bool:
    """True if ``doc_type`` is a cancelled/cured foreclosure or pure trustee admin.

    Apply AFTER a positive pre_foreclosure keyword match to reject the
    opposite-signal documents that substring-match the legit keywords.
    """
    if not doc_type:
        return False
    up = doc_type.upper()
    return any(tok in up for tok in _CANCELLATION_ADMIN)


# Organization-name tokens for the person-vs-company heuristic. Superset of the
# acclaimweb/idocmarket lists plus the WA foreclosure-trustee/servicer/law-firm
# vocabulary (a Notice of Trustee's Sale names a trustee like "QUALITY LOAN
# SERVICE CORP" / "NORTHWEST TRUSTEE SERVICES" / "MCCARTHY HOLTHUS PLLC" — never
# the homeowner). A name containing any of these whole-word tokens is treated as
# an organization, not a person.
#
# Bare "TRUST" is deliberately NOT here: a homeowner's revocable/family living
# trust ("DOE FAMILY TRUST") IS a valid distressed-owner lead. Institutional
# trusts always carry another token (BANK / NATIONAL / SERVICES / TRUSTEE /
# MORTGAGE / a securitization servicer), so they are still caught (Codex review).
_COMPANY_WORDS: frozenset[str] = frozenset({
    "INC", "LLC", "L.L.C", "LLP", "LP", "PLLC", "PC", "PS", "CORP",
    "CORPORATION", "COMPANY", "CO", "BANK", "INSURANCE", "MORTGAGE",
    "SYSTEMS", "FINANCIAL", "NATIONAL", "SERVICE", "SERVICES", "CLEARING",
    "REPUBLIC", "FARGO", "TITLE", "FEDERAL", "ASSOCIATION", "ASSN", "CREDIT",
    "UNION", "CAPITAL", "PARTNERS", "GROUP", "SOLUTIONS", "AGENCY", "AUTHORITY",
    "DEPARTMENT", "LOAN", "LOANS", "LENDING", "FUND", "FUNDING", "HOLDINGS",
    "ESCROW", "TRUSTEE", "RECONTRUST", "RECON", "AZTEC", "ZBS", "RCO",
    "ALDRIDGE", "PITE", "NA", "N.A", "MERS", "DBA", "LEGAL", "ATTORNEY",
    "ATTORNEYS", "STATE", "STATES", "COUNTY", "CITY",
})


# Known organization suffixes that the recorder sometimes spells letter-spaced
# ("COUGNIT L L C", "MCCARTHY HOLTHUS P L L C"), which would slip past the
# whole-word company-token check ("L L C" tokenizes to L/L/C, none == "LLC").
# Only these specific entity spellings are collapsed — NOT arbitrary single-letter
# runs — so a person's spaced middle initials are left intact (Codex review).
_SPACED_ORG_RE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bP\s+L\s+L\s+C\b"), "PLLC"),
    (re.compile(r"\bL\s+L\s+C\b"), "LLC"),
    (re.compile(r"\bL\s+L\s+P\b"), "LLP"),
    (re.compile(r"\bI\s+N\s+C\b"), "INC"),
    (re.compile(r"\bC\s+O\s+R\s+P\b"), "CORP"),
)


def is_person_name(name: str | None) -> bool:
    """True if ``name`` looks like a real person (a foreclosure homeowner lead).

    Rejects organization names (any company token, incl. letter-spaced entity
    suffixes like "L L C"), single-token strings (FIG / NA / MERS-style
    abbreviations), and very short strings. The collapsed form is used only for
    this classification — it is never returned or stored.
    """
    if not name:
        return False
    up = name.upper()
    for rx, repl in _SPACED_ORG_RE:
        up = rx.sub(repl, up)
    words = up.split()
    if any(w.strip(".,") in _COMPANY_WORDS for w in words):
        return False
    if len(words) < 2:
        return False
    return len(name) >= 5


def _persons_in(value: str | None) -> list[str]:
    """The person sub-names within a normalize_party_text() value.

    normalize_party_text joins structurally-stacked parties with ' / ', so one
    grantor cell can hold the borrower AND the trustee company
    ("BOYLE DAVID E / QUALITY LOAN SERVICE CORP"). Return only the person parts.
    """
    if not value:
        return []
    return [p.strip() for p in re.split(r"\s*/\s*", value) if is_person_name(p.strip())]


def orient_pre_foreclosure_party(
    grantor: str | None, grantee: str | None
) -> tuple[str, str | None] | None:
    """Return (party_name, heirs) with the BORROWER as party_name, or None.

    The borrower is whichever side carries a real person name (grantor on most
    filings; grantee on a trustee-indexed Notice of Trustee's Sale). If a side
    stacks the borrower with the trustee company, only the person sub-name(s)
    become party_name. Returns None when neither side carries a person — a
    bank-vs-trustee record with no homeowner lead (caller should drop it).
    """
    g_people = _persons_in(grantor)
    if g_people:
        return " / ".join(g_people), (grantee or None)
    e_people = _persons_in(grantee)
    if e_people:
        return " / ".join(e_people), (grantor or None)
    return None
