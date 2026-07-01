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

from src.scrapers.sources.nts_tacoma_index import parse_nts_notice

# Affinia is gated on the ORDERED no-colon header labels (Codex Q3) — never on a
# trustee name — so it can't fire on a colon layout (whose labels carry colons /
# the "of the Deed of Trust" variant).
_AFFINIA_SHAPE = re.compile(
    r"Grantor\(s\)\s+of\s+Deed\s+of\s+Trust\b[\s\S]{0,400}?Current\s+Beneficiary\b"
    r"[\s\S]{0,200}?Current\s+Trustee\b", re.I)

_AFF_GRANTOR = re.compile(
    r"Grantor\(s\)\s+of\s+Deed\s+of\s+Trust\s+(.+?)\s+Current\s+Beneficiary\b", re.I | re.S)
_AFF_BENEF = re.compile(r"Current\s+Beneficiary\s+(.+?)\s+Current\s+Trustee\b", re.I | re.S)
_AFF_TRUSTEE = re.compile(
    r"Current\s+Trustee\s+(.+?)\s+Current\s+(?:Mortgage|Loan)\s+Servicer\b", re.I | re.S)
_AFF_SERVICER = re.compile(
    r"Current\s+(?:Mortgage|Loan)\s+Servicer\s+(.+?)\s+Deed\s+of\s+Trust\s+Recording\b", re.I | re.S)
_AFF_DEEDREF = re.compile(
    r"Deed\s+of\s+Trust\s+Recording\s+Number\s*\(Ref\.?\s*#?\)?\s*(\d{6,})", re.I)
# Parcel must be EXACT digits/dashes only — a garbage parcel auto-matches at 0.90 (Codex).
_AFF_PARCEL = re.compile(r"Parcel\s+Number\(s\)\s+(\d[\d\-]{4,})\b", re.I)

# Surrogate-key sources for ANY King layout (used only when no trustee TS# was parsed).
_KING_DEEDREF_ANY = re.compile(
    r"(?:Deed\s+of\s+Trust\s+Recording\s+Number\s*\(Ref\.?\s*#?\)?|Instrument\s+No\.?)\s*(\d{8,})",
    re.I)
_KING_APN_ANY = re.compile(
    r"(?:\bAPN\b|Parcel\s+Number\(?s?\)?|Tax\s+Parcel(?:\s+Number)?)\s*[:#]?\s*(\d[\d\-]{4,})\b",
    re.I)


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

    # Surrogate ts_number when the notice carries no trustee TS# (King papers often
    # don't). REF-<deed recording #> is unique per deed of trust — an amended NTS for
    # the same loan collapses to one upserted row (latest wins), matching the existing
    # upsert semantics. APN-<parcel> is a DEGRADED fallback (repeat foreclosures on the
    # same parcel over time would collapse into one row); used only when no deed
    # reference exists. This is a CACHE dedup key, NOT a real trustee-sale id, and the
    # matcher never keys on ts_number (Codex Q2).
    if not parsed.get("ts_number"):
        ref = parsed.get("deed_reference")
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
