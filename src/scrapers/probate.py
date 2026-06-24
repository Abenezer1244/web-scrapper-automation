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
from enum import Enum

# --- Filing-agency / issuing-authority detection -------------------------------

# US state names + USPS abbreviations. The state regexes match ONLY real states
# (not any word ending in "STATE") so a co-decedent like "MCKINLEY STATE" / "JOHN
# STATE" is never mistaken for the issuing state (Codex review). Two-letter
# abbrevs come first; "WASH" covers the recorder's informal Washington abbrev.
_US_STATE = (
    r"(?:"
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO"
    r"|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
    r"|ALABAMA|ALASKA|ARIZONA|ARKANSAS|CALIFORNIA|COLORADO|CONNECTICUT|DELAWARE"
    r"|FLORIDA|GEORGIA|HAWAII|IDAHO|ILLINOIS|INDIANA|IOWA|KANSAS|KENTUCKY|LOUISIANA"
    r"|MAINE|MARYLAND|MASSACHUSETTS|MICHIGAN|MINNESOTA|MISSISSIPPI|MISSOURI|MONTANA"
    r"|NEBRASKA|NEVADA|NEW\s+HAMPSHIRE|NEW\s+JERSEY|NEW\s+MEXICO|NEW\s+YORK"
    r"|NORTH\s+CAROLINA|NORTH\s+DAKOTA|OHIO|OKLAHOMA|OREGON|PENNSYLVANIA"
    r"|RHODE\s+ISLAND|SOUTH\s+CAROLINA|SOUTH\s+DAKOTA|TENNESSEE|TEXAS|UTAH|VERMONT"
    r"|VIRGINIA|WASHINGTON|WASH|WEST\s+VIRGINIA|WISCONSIN|WYOMING"
    r")"
)

# The Certificate-of-Death / probate issuing authority a recorder may index as the
# grantor instead of the decedent: a Dept/Bureau/Office of Health (the dominant live
# shape) plus the vital-records-adjacent agencies seen across counties — Vital
# Records/Statistics, Licensing, Revenue, Social & Health Services. The optional
# leading state qualifier covers BOTH concatenated word orders the recorders emit:
# "STATE OF WA, DEPT OF HEALTH" (benton) and "WASHINGTON STATE DEPARTMENT OF HEALTH"
# (king). Every agency suffix is PHRASE-anchored ("(DEPT|BUREAU|OFFICE) OF <agency>")
# so no bare ambiguous token (REVENUE, LICENSING, BUREAU) can strip a real surname.
_AGENCY_DEPT_RE = re.compile(
    rf"(?:(?:STATE\s+OF\s+)?{_US_STATE}\.?\s*(?:STATE\b\s*)?,?\s*)?"
    r"(?:DEPARTMENT|DEPT|BUREAU|OFFICE)\.?\s+OF\s+"
    r"(?:HEALTH|LICENSING|REVENUE"
    r"|VITAL\s+(?:RECORDS|STATISTICS)"
    r"|SOCIAL\s+(?:AND|&)\s+HEALTH(?:\s+SERVICES)?)\b",
    re.IGNORECASE,
)

# A lone state name/abbreviation left as residue after the agency phrase is
# stripped ("WA DEPT OF HEALTH" -> "WA"). Treated as empty so the caller falls
# back to the grantee instead of surfacing "WA" as the party.
_LONE_STATE_RE = re.compile(r"^\s*(?:WA|WASH|WN|WASHINGTON)\.?\s*$", re.IGNORECASE)

# A WHOLE value that is a bare filing state, in either order:
#   "STATE OF WASHINGTON", "WASHINGTON STATE", "WASH. STATE OF", "STATE OF WA".
# Anchored ^...$ and matched per " / "-split segment, and restricted to REAL state
# names (_US_STATE) so it never fires on "WASHINGTON STATE UNIVERSITY" (trailing
# UNIVERSITY breaks the anchor) or a co-decedent like "MCKINLEY STATE". State-
# agnostic across all 50 so an out-of-state death certificate is corrected too.
_BARE_STATE_RE = re.compile(
    rf"^\s*(?:"
    rf"STATE\s+OF\s+{_US_STATE}"              # STATE OF <state>
    rf"|{_US_STATE}\.?\s+STATE(?:\s+OF)?"      # <state> STATE [OF]
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
    # "STATE OF" (the issuing-state phrase, incl. a comma-inverted "WASHINGTON,
    # STATE OF"). \bSTATE has no word boundary inside "eSTATE OF", so a decedent's
    # "ESTATE OF" caption is unaffected.
    r"|\bSTATE\s+OF\b"
    r"|\bVITAL\s+(?:RECORDS|STATISTICS)\b"
    # Death-care / vital-records institutions that index as the death-cert party.
    # Phrase-anchored (FUNERAL HOME, MEDICAL EXAMINER) or unambiguous occupational
    # words (MORTUARY, CREMATORY, CORONER) that are not used as personal surnames.
    r"|\bFUNERAL\s+(?:HOME|SERVICE|CHAPEL)\b|\bMORTUARY\b|\bCREMAT(?:ORY|ORIUM|ION)\b"
    r"|\bCORONER\b|\bMEDICAL\s+EXAMINER\b|\bDSHS\b"
    r"|\b(?:DEPARTMENT|DEPT|BUREAU|OFFICE)\s+OF\s+(?:LICENSING|REVENUE|VITAL|SOCIAL)\b"
    r"|\b(?:LLC|L\.?L\.?C|INC|CORP|CORPORATION|COMPANY|BANK|ESCROW|ASSOCIATION)\b"
    r"|\bTITLE\s+CO\b",
    re.IGNORECASE,
)

# A "LAST, FIRST..." comma-form personal name. A recorder indexes a real decedent
# as "SURNAME, GIVEN" — an institution is indexed as "<COUNTY> CORONER", "ACME
# FUNERAL HOME", "DEPARTMENT OF ...", never in comma-first-name form. So a comma-
# form value lacking an UNAMBIGUOUS org token is a person; this rescues a real
# surname that collides with an institution word ("CORONER, JANE", "BANK, JOHN")
# from being wrongly rejected by _NON_PERSON_RE (Codex review).
_PERSON_COMMA_RE = re.compile(r"^[A-Z][A-Za-z.'’-]+\s*,\s+[A-Z]")
# Tokens that NEVER form a personal surname/given-name — their presence in a comma-
# form value means it is an institution (or a comma-inverted state), not a person.
_UNAMBIGUOUS_ORG_RE = re.compile(
    r"\bDEPARTMENT\b|\bDEPT\b|\bBUREAU\b|\bOFFICE\s+OF\b|\bSTATE\s+OF\b"
    r"|\b(?:LLC|L\.?L\.?C|INC|CORP|CORPORATION|COMPANY|ESCROW|ASSOCIATION)\b"
    r"|\bFUNERAL\s+(?:HOME|SERVICE|CHAPEL)\b|\bMORTUARY\b|\bCREMAT(?:ORY|ORIUM|ION)\b"
    r"|\bMEDICAL\s+EXAMINER\b|\b(?:SUPERIOR|DISTRICT)\s+COURT\b|\bCLERK\s+OF\b"
    r"|\bUNIVERSITY\b|\bTITLE\s+CO\b|\bDSHS\b",
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
    cleaned = _AGENCY_DEPT_RE.sub("", name)
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
    # A stacked grantor where SOME (not all) " / "-segments are a bare filing state
    # ("DOE, JOHN / STATE OF WASHINGTON"): drop only the exact bare-state/lone-state
    # segments, keep the decedent. Limited to exact filing-state segments (Codex) so
    # a genuine co-decedent ("SMITH JOHN / SMITH JANE") is never dropped.
    if len(parts) > 1:
        kept = [p for p in parts if not _BARE_STATE_RE.match(p) and not _LONE_STATE_RE.match(p)]
        if kept and len(kept) != len(parts):
            cleaned = " / ".join(kept)
            parts = kept
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
    """True if ``value`` looks like a real person/decedent (not an agency/court/org).

    A "LAST, FIRST" comma-form value with no UNAMBIGUOUS org token is treated as a
    person even if it contains an institution-word surname ("CORONER, JANE"); only
    the non-comma institutional form ("PIERCE COUNTY CORONER") is rejected.
    """
    if not value or not value.strip():
        return False
    if is_filing_agency_party(value):
        return False
    v = value.strip()
    if _PERSON_COMMA_RE.match(v) and not _UNAMBIGUOUS_ORG_RE.search(v.upper()):
        return True
    return not _NON_PERSON_RE.search(v.upper())


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


# --- Probate signal classification (lead-subtype labeling) ----------------------
#
# The live verification (2026-06-23) found ~47% of "probate" leads were actually
# Transfer-on-Death deeds CREATED by a LIVING owner — a softer estate-planning
# signal, NOT a death/inheritance event. Selling those as "probate (deceased
# owner)" is a mislabel. This classifier sorts each document into an honest
# subtype so the pipeline can tag every probate lead (and optionally let the
# customer include/exclude the living-owner TOD bucket). It NEVER drops on its own
# — it labels; callers decide what to keep.


class ProbateSignal(str, Enum):
    """Honest subtype of a probate-record-type document.

    ``str`` mixin so ``.value`` is the stable slug the pipeline stores in
    ``enrichment_data["lead_subtype"]`` and exports as a CSV column.
    """

    DEATH_INHERITANCE = "probate_death_inheritance"          # real death / inheritance
    TOD_LIVING_OWNER = "tod_living_owner_estate_planning"    # LIVING owner TOD planning
    NONPROBATE_TRANSFER = "nonprobate_transfer"              # Lack-of-Probate affidavit
    UNKNOWN = "unknown_probate"                              # no confident signal


# A Transfer-on-Death instrument by any of the names recorders use. "DEATH DEED"
# is the colloquial TOD label some portals emit (Codex: don't require the exact
# "TRANSFER ON DEATH" phrase). \bTOD\b is word-anchored so it never fires inside a
# surname like "TODD".
_TOD_DOC_RE = re.compile(
    r"TRANSFER\s+ON\s+DEATH|\bTOD\b|\bDEATH\s+DEED\b", re.IGNORECASE
)

# Markers that PROVE a death / inheritance / post-death effectuation. When ANY of
# these is present the document is a real lead even if it also names a TOD (a
# death-triggered TOD effectuation: "Affidavit to Perfect TOD", "Death Cert to
# Perfect Death Deed", "TOD Beneficiary Affidavit"). Phrase-anchored so no bare
# ambiguous token promotes a living-owner deed.
_DEATH_MARKER_RE = re.compile(
    r"DEATH\s+CERT|CERTIFICATE\s+OF\s+DEATH|\bDECEASED\b|\bDECEDENT\b"
    r"|AFFIDAVIT\s+OF\s+DEATH|DEATH\s+OF\s+(?:TRANSFEROR|GRANTOR|OWNER)"
    r"|PROOF\s+OF\s+DEATH|EVIDENCE\s+OF\s+DEATH|TRANSFEROR\s+DECEASED"
    r"|\bPERFECT|\bEFFECTUAT"
    r"|LETTERS\s+TESTAMENTARY|LETTERS\s+OF\s+ADMINISTRATION|\bTESTAMENTARY\b"
    r"|PERSONAL\s+REPRESENTATIVE|PERSONAL\s+REP\b"
    r"|ADMINISTRAT(?:OR|RIX)|EXEC(?:UTOR|UTRIX)"
    r"|AFFIDAVIT\s+OF\s+(?:HEIRSHIP|SUCCESSOR)|\bHEIRSHIP\b|\bHEIRS?\b|\bINHERITANCE\b"
    # WILL / TESTAMENT as words so multi-word/punctuated labels classify too
    # ("LAST WILL AND TESTAMENT", "WILL/TESTAMENT" -> normalized "WILL TESTAMENT").
    # Safe within the probate-gated caller: a TOD deed never contains these (Codex P2).
    r"|\bWILL\b|\bTESTAMENT\b"
    r"|BENEFICIARY\s+AFFIDAVIT|DECREE\s+OF\s+DISTRIBUTION|ESTATE\s+OF",
    re.IGNORECASE,
)

_LACK_OF_PROBATE_RE = re.compile(r"LACK\s+OF\s+PROBATE", re.IGNORECASE)
_BARE_PROBATE_RE = re.compile(r"\bPROBATE\b", re.IGNORECASE)

# Abbreviated probate/death doc CODES that some portals emit as the WHOLE doc_type
# (Codex P2): EagleWeb/Clallam -> DEATH, LETTR, EXEC, SUCC; AcclaimWeb/Chelan ->
# DEATH, AFFD, PTREC. Matched by EXACT whole (normalized) value — NOT substring/word
# — so the bare "DEATH" code is recognized as a death while "TRANSFER ON DEATH DEED"
# (multi-word) stays on the TOD path. "TOD" is absent here on purpose: a bare TOD code
# is a living-owner deed, handled by _TOD_DOC_RE. (WILL/HEIR are NOT here — they are
# words that also appear in multi-word labels, so _DEATH_MARKER_RE handles them.)
_ABBREV_DEATH_CODES: frozenset[str] = frozenset(
    {"DEATH", "LETTR", "EXEC", "SUCC", "AFFD", "PTREC"}
)


def _normalize_doc_type(doc_type: str) -> str:
    """Take the document label before any concatenated recording number.

    Live recorder strings append the recording id after a newline
    ("Transfer on Death Deed\\n2026-1482913"); keep only the label, uppercased and
    whitespace-collapsed, so the matchers see a clean type.
    """
    head = doc_type.split("\n", 1)[0]
    # Treat hyphens/slashes as word separators so the phrase matchers see a canonical
    # spaced form: "Lack-of-Probate Affidavit" -> "LACK OF PROBATE AFFIDAVIT",
    # "Transfer-on-Death Deed" -> "TRANSFER ON DEATH DEED" (Codex P2 — hyphenated
    # recorder labels were falling through to the bare-PROBATE rule and mislabeling
    # Lack-of-Probate as a death/inheritance lead).
    return re.sub(r"[\s\-/]+", " ", head).strip().upper()


def classify_probate_signal(doc_type: str | None) -> ProbateSignal:
    """Sort a probate document-type string into an honest ``ProbateSignal``.

    Order matters:
      1. Lack-of-Probate affidavit -> NONPROBATE_TRANSFER (checked before the bare
         "PROBATE" rule, which its text would otherwise trip).
      2. A TOD instrument WITH a death/effectuation marker -> DEATH_INHERITANCE
         (death-triggered TOD); WITHOUT one -> TOD_LIVING_OWNER (living owner).
      3. Any other death/probate marker -> DEATH_INHERITANCE.
      4. A bare "PROBATE" label (e.g. Pierce ARMS) -> DEATH_INHERITANCE.
      5. Anything else (e.g. Community Property Agreement) -> UNKNOWN — labeled
         honestly, never faked into a death.

    Pure function: classifies only, never drops. Callers gate on the result.
    """
    if not doc_type or not doc_type.strip():
        return ProbateSignal.UNKNOWN

    up = _normalize_doc_type(doc_type)

    if _LACK_OF_PROBATE_RE.search(up):
        return ProbateSignal.NONPROBATE_TRANSFER

    # Death if a phrase marker matches OR the WHOLE value is an abbreviated death
    # code. Exact-value for the codes so it can't fire inside a multi-word TOD label.
    has_death = bool(_DEATH_MARKER_RE.search(up)) or up in _ABBREV_DEATH_CODES

    if _TOD_DOC_RE.search(up):
        return ProbateSignal.DEATH_INHERITANCE if has_death else ProbateSignal.TOD_LIVING_OWNER

    if has_death:
        return ProbateSignal.DEATH_INHERITANCE

    if _BARE_PROBATE_RE.search(up):
        return ProbateSignal.DEATH_INHERITANCE

    return ProbateSignal.UNKNOWN


def is_living_owner_tod(doc_type: str | None) -> bool:
    """True if ``doc_type`` is a LIVING-owner Transfer-on-Death planning doc.

    Convenience predicate over :func:`classify_probate_signal` for the connector
    filter: these are the rows excluded from probate unless the customer opts into
    the estate-planning signal. Death-triggered TOD effectuations return False
    (they are real ``DEATH_INHERITANCE`` leads).
    """
    return classify_probate_signal(doc_type) is ProbateSignal.TOD_LIVING_OWNER


# Signal strength for merging a doc_type result with a recorder-comment result —
# lower = stronger. A row keeps the STRONGEST signal across its two fields.
_SIGNAL_PRIORITY: dict[ProbateSignal, int] = {
    ProbateSignal.DEATH_INHERITANCE: 0,
    ProbateSignal.NONPROBATE_TRANSFER: 1,
    ProbateSignal.TOD_LIVING_OWNER: 2,
    ProbateSignal.UNKNOWN: 3,
}


def classify_probate_signal_for_row(
    doc_type: str | None, comment: str | None = None
) -> ProbateSignal:
    """Classify a probate row from its doc_type AND recorder comment together.

    Recorders split the signal across the two fields, so neither alone is enough:
      - Skagit keeps a generic "Affidavit" whose probate signal is in the comment
        ("LACK OF PROBATE AFFIDAVIT" / "INHERITANCE") — the comment must be consulted.
      - A "Transfer on Death Deed" doc_type with an "Affidavit of Death" / "Beneficiary
        Affidavit" comment is a death-TRIGGERED TOD (a real inheritance), not a
        living-owner deed — the comment's death marker must UPGRADE it.

    So we classify each field and keep the STRONGER signal
    (death > nonprobate > tod > unknown). This rescues a comment-only signal AND
    upgrades a death-triggered TOD, while never letting the comment DOWNGRADE a
    confident doc_type (Codex P2).
    """
    base = classify_probate_signal(doc_type)
    if not comment or not comment.strip():
        return base
    from_comment = classify_probate_signal(comment)
    return min(base, from_comment, key=_SIGNAL_PRIORITY.__getitem__)


def should_include_probate_row(
    record_type: str | None,
    include_living_owner_tod: bool | None,
    doc_type: str | None,
    comment: str | None = None,
) -> bool:
    """Phase 3 worker filter: keep this row in the delivered probate lead set?

    Tri-state ``include_living_owner_tod`` (persisted on ``ScraperConfig``):
      - ``None``  legacy / grandfathered  -> include everything (Phase 2 labels it).
      - ``False`` new probate default      -> exclude LIVING-owner TOD planning docs.
      - ``True``  explicit customer opt-in -> include everything.

    Drops a row ONLY when it is a living-owner TOD AND the flag is exactly ``False``.
    Death, death-triggered TOD (a recorder comment upgrades it), nonprobate, and
    unknown signals always survive — the filter narrows honestly, it never widens a
    death claim. No-op for non-probate record types.

    Uses :func:`classify_probate_signal_for_row` (doc_type + comment) rather than the
    bare doc_type predicate so a death marker in the comment rescues a real lead.
    """
    if record_type != "probate":
        return True
    if include_living_owner_tod is not False:
        return True
    return (
        classify_probate_signal_for_row(doc_type, comment)
        is not ProbateSignal.TOD_LIVING_OWNER
    )


def new_probate_config_tod_default(
    record_type: str | None, provided: bool | None
) -> bool | None:
    """Resolve ``include_living_owner_tod`` for a NEWLY created scraper config.

    New probate configs exclude living-owner TOD by default (probate = death), so an
    omitted / null choice resolves to ``False``. An explicit ``True``/``False`` from
    the client is honored. Non-probate configs are unaffected — the flag stays
    whatever was provided (``None`` for the common case); it only governs probate.

    Grandfathering (``None`` => include) is for PRE-EXISTING configs only. This makes
    ``None`` on a stored row unambiguous: "created before the toggle existed" — never
    "a new config that happens to want everything". Shared by the create, preview, and
    batch paths so their default can never drift.
    """
    if record_type == "probate" and provided is None:
        return False
    return provided


def effective_tod_on_update(
    field_provided: bool, provided_value: bool | None, stored_value: bool | None
) -> bool | None:
    """Resolve ``include_living_owner_tod`` on a PATCH edit.

    An OMITTED field — or an EXPLICIT ``null``, which is not a user-selectable state
    (``None`` marks pre-toggle configs only) — preserves the stored value, so editing
    a config never silently flips its TOD policy. In particular a
    ``"include_living_owner_tod": null`` PATCH must NOT downgrade a ``False``/``True``
    probate config back to grandfathered/include-all and re-enable living-owner TOD
    without an explicit ``true`` opt-in (Codex P2). A real ``True``/``False`` replaces it.
    """
    if field_provided and provided_value is not None:
        return provided_value
    return stored_value
