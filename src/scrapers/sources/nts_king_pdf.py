"""King County (Queen Anne & Magnolia News PDF) NTS field parser.

The King papers publish trustee sales in layouts the shared Tacoma/Snohomish parser
does not fully handle:
  - AFFINIA: NO-COLON labels ("Grantor(s) of Deed of Trust <x> Current Beneficiary
    <y> Current Trustee <z> ...") which the colon field regexes mis-read into garbage.
  - MTC / others: colon labels the shared parser handles, but with NO "T.S. No." label.
Both use month-name auction dates (handled in nts_tacoma_index — Step A).

This module WRAPS the shared ``parse_nts_notice`` with King-specific extraction plus a
SURROGATE ``ts_number``: the ``nts_notices`` natural key is (source, ts_number), and a
NULL breaks upsert dedup. Isolated by design — the shared parser and the Tacoma
(Pierce) + Snohomish paths are untouched; the King crawler task passes
``parse_king_notice`` as ``parse_fn`` (Codex).
"""
import re

from src.scrapers.sources.nts_tacoma_index import _clean_address, parse_nts_notice

# Affinia is gated on the ORDERED no-colon header labels (Codex Q3) — never on a
# trustee name — so it can't fire on a colon layout (whose labels carry colons /
# the "of the Deed of Trust" variant).
# The negative lookaheads (?!\s*:) and (?!\s+of\s+the\s+Deed) make the gate reject the
# COLON layout's "…: value" and "…of the Deed of Trust:" labels, so it can never fire
# on an MTC/colon block and clobber its correctly colon-parsed fields (Codex).
# Gap bounds are generous on purpose (Codex 2026-07-01): grantor lists run long and
# securitization-trust beneficiary names ("Wilmington Trust … as Trustee to Lehman XS
# Trust … Series 2006-5") exceed 200 chars in the wild — a live notice was lost to a
# {0,200} bound. The lookaheads, not the bounds, are what keep colon layouts out.
_AFFINIA_SHAPE = re.compile(
    r"Grantor\(s\)\s+of\s+Deed\s+of\s+Trust(?!\s*:)\b[\s\S]{0,800}?"
    r"Current\s+Beneficiary(?!\s*:)(?!\s+of\s+the\s+Deed)\b[\s\S]{0,1000}?"
    r"Current\s+Trustee(?!\s*:)(?!\s+of\s+the\s+Deed)\b", re.I)

# Value regexes carry the SAME negative guards (defense-in-depth: even if the gate
# ever passed on a mixed block, these won't capture a "of the Deed of Trust:" label).
_AFF_GRANTOR = re.compile(
    r"Grantor\(s\)\s+of\s+Deed\s+of\s+Trust(?!\s*:)\s+(.+?)\s+Current\s+Beneficiary\b", re.I | re.S)
_AFF_BENEF = re.compile(
    r"Current\s+Beneficiary(?!\s*:)(?!\s+of\s+the\s+Deed)\s+(.+?)\s+Current\s+Trustee\b", re.I | re.S)
_AFF_TRUSTEE = re.compile(
    r"Current\s+Trustee(?!\s*:)(?!\s+of\s+the\s+Deed)\s+(.+?)\s+Current\s+(?:Mortgage|Loan)\s+Servicer\b",
    re.I | re.S)
_AFF_SERVICER = re.compile(
    r"Current\s+(?:Mortgage|Loan)\s+Servicer(?!\s*:)\s+(.+?)\s+Deed\s+of\s+Trust\s+Recording\b",
    re.I | re.S)
_AFF_DEEDREF = re.compile(
    r"Deed\s+of\s+Trust\s+Recording\s+Number\s*\(Ref\.?\s*#?\)?\s*(\d{6,})", re.I)
# Parcel must be an EXACT digits/dashes token — a garbage parcel auto-matches at 0.90
# (Codex). (?![-\w]) (not \b) rejects "12345-EXHIBIT" -> would else capture "12345".
_AFF_PARCEL = re.compile(r"Parcel\s+Number\(s\)\s+(\d[\d\-]{4,})(?![-\w])", re.I)

# Surrogate-key sources for ANY King layout (used only when no trustee TS# was parsed).
_KING_DEEDREF_ANY = re.compile(
    r"(?:Deed\s+of\s+Trust\s+Recording\s+Number\s*\(Ref\.?\s*#?\)?|Instrument\s+No\.?"
    # Label-anchored prose form (Tribune 2025-12-17): only the number printed right after
    # THIS label, never a bare "Auditor's File No." (an assignment carries that too).
    r"|Reference\s+number\s+of\s+(?:the\s+)?deed\s+of\s+trust\s*:\s*(?:Auditor'?s\s+File\s+No\.?)?)"
    r"\s*(\d{8,})",
    re.I)
_KING_APN_ANY = re.compile(
    r"(?:\bAPN\b|Parcel\s+Number\(?s?\)?|Tax\s+Parcel(?:\s+Number)?)\s*[:#]?\s*(\d[\d\-]{4,})(?![-\w])",
    re.I)


# ── Commercial-loan colon header (live 2026-09-09, Snohomish Tribune, Burns Law PLLC):
#   "NOTICE OF TRUSTEE'S SALE OF COMMERCIAL LOAN PURSUANT TO … GRANTOR(S): <owner>
#    BENEFICIARY/ GRANTEE: <lender> TRUSTEE: <trustee> ABBREV. LEGAL: <legal>
#    PARCEL NO(S).: <apn> REFERENCE NO. (DOT): <recording #>"
# and later in the body "Situs Address: <addr> Abbrev. Legal: …". The shared colon
# regexes misread it: "BENEFICIARY/" is not a stop, so the grantor ran into the next
# label, the beneficiary ran to section I, and "PARCEL NO(S)." / "REFERENCE NO. (DOT)" /
# "Situs Address" are labels no shared pattern knows. There is no trustee sale number
# (only the firm's "BL #" file number, which is not one), so identity comes from the
# deed of trust recording number, exactly like Affinia.
# Gated on the ALL-CAPS title plus the three ordered header labels, so it cannot fire on
# any other layout. Values are colon-free and length-bounded, so a capture can never run
# across a label; the address is filled only when the shared parser found none.
_COMMERCIAL_SHAPE = re.compile(
    r"NOTICE\s+OF\s+TRUSTEE'S\s+SALE\s+OF\s+COMMERCIAL\s+LOAN\b[^:]{0,300}"
    r"GRANTOR\(S\)\s*:[^:]{1,600}BENEFICIARY\s*/\s*GRANTEE\s*:[^:]{1,600}TRUSTEE\s*:")
_COM_GRANTOR = re.compile(r"GRANTOR\(S\)\s*:\s*([^:]{1,600}?)\s+BENEFICIARY\s*/\s*GRANTEE\s*:")
_COM_BENEF = re.compile(r"BENEFICIARY\s*/\s*GRANTEE\s*:\s*([^:]{1,600}?)\s+TRUSTEE\s*:")
_COM_TRUSTEE = re.compile(
    r"\bTRUSTEE\s*:\s*([^:]{1,300}?)\s+(?:ABBREV\.?\s+LEGAL|PARCEL\s+NO\(S\)\.?)\s*:")
_COM_PARCEL = re.compile(r"PARCEL\s+NO\(S\)\.?\s*:\s*(\d[\d\-]{4,})(?![-\w])")
_COM_DEEDREF = re.compile(r"REFERENCE\s+NO\.?\s*\(DOT\)\s*:\s*(\d{8,14})(?!\d)")
_COM_SITUS = re.compile(r"Situs\s+Address\s*:\s*([^:]{5,200}?)\s+Abbrev\.?\s+Legal\s*:", re.I)


def _clean(v: str | None) -> str | None:
    return " ".join(v.split()).strip().rstrip(".,") or None if v else None


def parse_king_notice(block: str) -> dict:
    """Parse one King PDF notice block; drop-in for ``parse_nts_notice`` in the crawler.

    Reuses the shared parser (month-name auction, property_address, principal, and the
    colon fields for MTC blocks), overrides the Affinia no-colon fields, and guarantees
    a ``ts_number`` (real, else a REF-/APN- surrogate) so the (source, ts_number) upsert
    key dedups. Returns None-valued fields the shared parser returns; never raises.
    """
    parsed = parse_nts_notice(block)
    text = block.replace("’", "'").replace("‘", "'").replace("�", "'")

    # Affinia no-colon override — ONLY when the ordered Affinia labels are present.
    if _AFFINIA_SHAPE.search(text):
        for key, rx in (("grantor", _AFF_GRANTOR), ("beneficiary", _AFF_BENEF),
                        ("trustee", _AFF_TRUSTEE), ("servicer", _AFF_SERVICER)):
            m = rx.search(text)
            if m:
                parsed[key] = _clean(m.group(1))
        m = _AFF_DEEDREF.search(text)
        if m:
            parsed["deed_reference"] = m.group(1)
        m = _AFF_PARCEL.search(text)
        if m:
            parsed["parcel"] = m.group(1)

    # Commercial-loan colon header override, ONLY when that exact header is present.
    if _COMMERCIAL_SHAPE.search(text):
        for key, rx in (("grantor", _COM_GRANTOR), ("beneficiary", _COM_BENEF),
                        ("trustee", _COM_TRUSTEE)):
            m = rx.search(text)
            parsed[key] = _clean(m.group(1)) if m else None  # never keep the misread
        m = _COM_DEEDREF.search(text)
        if m:
            parsed["deed_reference"] = m.group(1)
        m = _COM_PARCEL.search(text)
        if m:
            parsed["parcel"] = m.group(1)
        if not parsed.get("property_address"):
            m = _COM_SITUS.search(text)
            if m:
                parsed["property_address"] = _clean_address(m.group(1))

    # Surrogate ts_number when the notice carries no trustee TS# (King papers often
    # don't). REF-<deed recording #> is unique per deed of trust — an amended NTS for
    # the same loan collapses to one upserted row (latest wins), matching the existing
    # upsert semantics. APN-<parcel> is a DEGRADED fallback (repeat foreclosures on the
    # same parcel over time would collapse into one row); used only when no deed
    # reference exists. This is a CACHE dedup key, NOT a real trustee-sale id, and the
    # matcher never keys on ts_number (Codex Q2).
    if not parsed.get("ts_number"):
        ref = parsed.get("deed_reference")
        # A recording number is digits. The shared _DEED_REF reads "Reference number of
        # the deed of trust: Auditor's File No. 202303210039" as "Auditor" (live, Tribune
        # 2025-12-17); as a key, REF-Auditor would merge every notice printed that way.
        if ref and not ref.isdigit():
            ref = None
        # The same prose layout ("The grantor of the deed of trust is X. The current
        # beneficiary … is Y. … Reference number of the deed of trust: Auditor's File
        # No. …") has no colon after either name, so the shared colon regexes both run
        # to that first colon and return the SAME text. An owner is never their own
        # lender: identical values are a misread, and a blank name beats a wrong one.
        if parsed.get("grantor") and parsed.get("grantor") == parsed.get("beneficiary"):
            parsed["grantor"] = parsed["beneficiary"] = None
        if not ref:
            m = _KING_DEEDREF_ANY.search(text)
            ref = m.group(1) if m else None
        if ref:
            parsed["ts_number"] = f"REF-{ref}"[:64]
        else:
            apn = parsed.get("parcel")
            if not apn:
                m = _KING_APN_ANY.search(text)
                apn = m.group(1) if m else None
            if apn:
                parsed["ts_number"] = f"APN-{apn}"[:64]
    return parsed


def parse_snoho_notice(block: str) -> dict:
    """parse_king_notice for the Snohomish Tribune, minus the APN- surrogate.

    APN-<parcel> is not collision-free: two different sales on one parcel (a first and
    a second lien) would collapse into one (source, ts_number) row. King keeps its APN-
    keys because production rows already use them and re-keying would orphan them; the
    Tribune never stored one, so here a notice whose ONLY identity is its parcel is left
    without a ts_number and is_valid_nts rejects it. REF-<deed recording #> is unique
    per deed of trust and stays.
    """
    parsed = parse_king_notice(block)
    if (parsed.get("ts_number") or "").startswith("APN-"):
        parsed["ts_number"] = None
    return parsed
