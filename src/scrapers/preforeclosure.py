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


# Known WA/national foreclosure-TRUSTEE brand phrases that carry NO standard
# company token, so the whole-word check below misses them and they slip into
# party_name as a fake "person" (e.g. "WESTERN PROGRESSIVE-WASHINGTON", a national
# substitute-trustee). These are never homeowners. Matched as a substring on the
# upper-cased name. Keep this list to genuinely token-less trustee brands — every
# other trustee/servicer ("QUALITY LOAN…", "TRUSTEE CORPS", "CLEAR RECON",
# "NORTHWEST TRUSTEE") already carries a _COMPANY_WORDS token.
# Word-boundary anchored so it can only match the standalone brand, never a phrase
# that happens to span two unrelated name tokens.
_KNOWN_ORG_PHRASE_RE = re.compile(r"\bWESTERN\s+PROGRESSIVE\b")


# Vesting / tenancy boilerplate that trails an owner name in a Notice of Trustee
# Sale grantor line ("MICHAEL A. BRANDT, A MARRIED MAN, AS HIS SOLE AND SEPARATE
# PROPERTY"). It is legal vesting language, not part of the name, and pollutes the
# displayed lead + the skip-trace key. Removed; the " AND " connector BETWEEN two
# owner names is preserved so co-borrowers survive.
_VESTING_CLAUSE = re.compile(
    r"\s*,?\s*(?:"
    r"AN?\s+(?:MARRIED|UNMARRIED|WIDOWED|DIVORCED|SINGLE)\s+(?:MAN|WOMAN|PERSON|COUPLE)\b"
    r"|A\s+SINGLE\s+(?:MAN|WOMAN|PERSON)\b"
    r"|AS\s+(?:HIS|HER|THEIR|ITS)\s+(?:SOLE\s+AND\s+)?SEPARATE\s+(?:PROPERTY|ESTATE)\b"
    r"|(?:AS\s+)?(?:HUSBAND\s+AND\s+WIFE|WIFE\s+AND\s+HUSBAND)\b"
    r"|AS\s+JOINT\s+TENANTS(?:\s+WITH\s+(?:THE\s+)?RIGHTS?\s+OF\s+SURVIVORSHIP)?\b"
    r"|AS\s+TENANTS\s+IN\s+COMMON\b"
    r")",
    re.I,
)


# A second NTS layout appends downstream labels onto the grantor NAME
# ("<owner> Grantee(s): <trustee> ... Original beneficiary of the deed of trust:
# <lender>"). Truncate at the first such label so the lead name is just the owner.
# Colon-anchored and deliberately NOT "as Trustee" — a real owner can legitimately be
# "JOHN DOE, AS TRUSTEE OF THE DOE FAMILY TRUST" (Codex). The parser _STOP fix keeps
# fresh grantors clean; this also cleans already-cached nts_notices.grantor at read time.
_TRAILING_LABEL = re.compile(
    r"\s+(?:Grantee\(?s?\)?\s*:|Original\s+beneficiary(?:\s+of\s+the\s+deed\s+of\s+trust)?\s*:)",
    re.I,
)


def strip_trailing_labels(name: str | None) -> str | None:
    """Cut a grantor name at the first bled-in downstream NTS label (Grantee(s)/
    Original beneficiary). Returns the input unchanged when no such label is present."""
    if not name:
        return name
    return _TRAILING_LABEL.split(name, maxsplit=1)[0].strip() or None


def strip_vesting_clause(name: str | None) -> str | None:
    """Remove trailing vesting/tenancy boilerplate from an NTS grantor name.

    'MICHAEL A. BRANDT, A MARRIED MAN, AS HIS SOLE AND SEPARATE PROPERTY'
        -> 'MICHAEL A. BRANDT'
    'VADAD SOLEIMANZADEH, AN UNMARRIED PERSON, AND YAHYA KAZEMIKARANI, AN UNMARRIED PERSON'
        -> 'VADAD SOLEIMANZADEH AND YAHYA KAZEMIKARANI'
    Returns the input unchanged when no vesting phrase is present.
    """
    if not name:
        return name
    name = strip_trailing_labels(name)  # drop bled-in Grantee(s)/beneficiary labels first
    if not name:
        return name
    s = _VESTING_CLAUSE.sub("", name)
    s = re.sub(r",\s*,", ",", s)            # collapse the comma left where a clause was
    s = re.sub(r",\s*(?=AND\b)", " ", s, flags=re.I)  # ", AND NAME2" -> " AND NAME2"
    s = re.sub(r"[\s,]+$", "", s)           # trailing commas/space
    s = re.sub(r"^[\s,]+", "", s)           # leading commas/space
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip() or None


def is_person_name(name: str | None) -> bool:
    """True if ``name`` looks like a real person (a foreclosure homeowner lead).

    Rejects organization names (any company token, incl. letter-spaced entity
    suffixes like "L L C", and known token-less trustee brand phrases),
    single-token strings (FIG / NA / MERS-style abbreviations), and very short
    strings. The collapsed form is used only for this classification — it is never
    returned or stored.
    """
    if not name:
        return False
    up = name.upper()
    if _KNOWN_ORG_PHRASE_RE.search(up):
        return False
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
