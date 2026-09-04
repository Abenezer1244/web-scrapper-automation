"""King County parcel repair for a MALFORMED recorder PID.

WHY: King's recorder prints the parcel in its legal-description index, and it
occasionally prints one that is not a valid King PIN — "PID: 64116000027" is 11
digits where a King PIN is exactly 10 (6-digit major + 4-digit minor). We store
what the county printed (never overwrite a source value with a derived one), but
that value cannot be looked up: eRealProperty SILENTLY TRUNCATES it to the first
10 digits and serves a DIFFERENT parcel, so ``parcel_page_is_for`` now rejects
the page and the lead ships with no address at all — which
``actionable_condition`` then drops from the customer-facing list.

This module recovers the REAL parcel so the lead can be enriched, WITHOUT
touching the stored ``parcel_id``. Design settled with Codex:

    Result.parcel_id            = SOURCE identity. Immutable, exactly what the
                                  recorder printed. Feeds the FROZEN dedup_hash
                                  (billing) and source_fingerprint (idempotency).
    enrichment_data.resolved_*  = CANONICAL PROPERTY identity. What we verified
                                  the parcel actually is, plus the evidence that
                                  chose it. Used for lookups and display.

Rewriting ``parcel_id`` instead would turn a county typo repair into a
billing/idempotency migration: dedup_hash is the key ``delivered_records`` is
built on, so a changed hash makes an already-delivered lead look undelivered and
it can be BILLED A SECOND TIME. That is the wrong coupling.

GUARDS (pressure-tested with Codex — this must never mis-attach a neighbouring
property, which is the exact defect it exists to fix):

  1. Fires ONLY on a CONFIRMED mismatch: the stored id is not a well-formed
     10-digit King PIN and the direct lookup was already rejected by
     ``parcel_page_is_for``. Never on a transient error.
  2. Candidates come from DELETION only — the observed defect is an extra
     character. Substitutions and transpositions are deliberately NOT modelled:
     a substituted digit can be a REAL different parcel that eRealProperty will
     honestly echo, so this guard cannot detect it, and widening the space turns
     typo repair into fuzzy parcel guessing.
  3. A candidate must EXIST in King's strict ArcGIS parcel layer. ZERO survivors
     aborts — which is also what a GIS outage produces, so a transient failure
     can never be mistaken for a hard negative (all candidates travel in one
     request, so a partial answer is not possible).
  4. A single survivor is accepted on its own ONLY when the candidate space was
     EXHAUSTIVE (the 11-digit case). The 12-digit space is a bounded guess at the
     defect shape, so a lone survivor there could be the truncation parcel while
     the real one needed a deletion the set never generated (Codex P1) — that
     case falls through to guard 5.
  5. Otherwise the ONLY permitted tie-breaker is an assessor owner naming the same
     PERSON as the lead's own party, compared as WHOLE token sequences: every
     shared position must agree (a single letter matches an initial), and at least
     two positions must be shared. Surname-only, initial-only, fuzzy spellings and
     entity names are all rejected, and two matching candidates abort. Assessor lag
     (title already transferred) makes this rule MISS, which is safe; it must never
     make it guess.
  6. The accepted candidate is re-verified with ``parcel_page_is_for`` before use.
  7. Full provenance is returned; nothing is invented.

Live evidence (2026-09-03) for instrument 20260715000926, decedent REINKE NORMAN
LEONARD, recorder PID 64116000027: 7 distinct deletion candidates, of which
exactly THREE exist in King GIS — 6411600002 (SNYDER JACOB), 6411600007 (ENTROP
RICHARD+SHARYN R) and 6411600027 (REINKE NORMAN L). GIS existence alone does not
disambiguate; the owner match picks exactly one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_parcel")

KING_PIN_DIGITS = 10

# Name suffixes that are not part of the identity.
_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV", "V", "MD", "DDS", "ESQ", "PHD"})

# A value that names an organisation, not a person. Never usable as an owner
# match (Codex): an entity name says nothing about which candidate is the lead's.
_ENTITY_RE = re.compile(
    r"\b(?:LLC|L\.?L\.?C|INC|CORP|CORPORATION|COMPANY|CO|LP|LLP|BANK|TRUST|TRUSTEE"
    r"|ESTATE|ASSOCIATION|ASSN|PROPERTIES|PARTNERS|HOLDINGS|CHURCH|CITY|COUNTY"
    r"|DEPARTMENT|DEPT|STATE\s+OF|UNIVERSITY|DISTRICT|AUTHORITY|FOUNDATION)\b",
    re.IGNORECASE,
)

# Co-owner separators King and the recorder use: "TRUJILLO CHUCK+PATSY",
# "JIAO ALEX ZIHENG & YIP WAN", "BASS GEORGANNA K / WARREN GEORGANNA K".
_COOWNER_SPLIT_RE = re.compile(r"\s*(?:\+|/|&|\bAND\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedParcel:
    """A verified replacement PIN plus the evidence that chose it."""

    parcel_id: str
    method: str                       # "gis_singleton" | "gis_plus_owner_match"
    candidates_generated: int
    candidates_in_gis: tuple[str, ...] = ()
    matched_owner: str | None = None
    matched_party: str | None = None

    def provenance(self, source_pid: str) -> dict:
        """The blob stored on ``enrichment_data`` — auditable without re-running."""
        return {
            "resolved_parcel_id": self.parcel_id,
            "source_parcel_id": source_pid,
            "resolved_by": self.method,
            "resolved_candidates_generated": self.candidates_generated,
            "resolved_candidates_in_gis": list(self.candidates_in_gis),
            "resolved_owner_match": self.matched_owner,
            "resolved_party_match": self.matched_party,
        }


def is_well_formed_king_pin(value: str | None) -> bool:
    """True if ``value`` is exactly the 10 digits a King PIN must be."""
    return bool(value) and re.fullmatch(r"\d{10}", value.strip() or "") is not None


def candidate_pins(raw: str | None) -> list[str]:
    """Bounded deletion candidates that reduce ``raw`` to a 10-digit King PIN.

    11 digits -> every distinct single-deletion result.
    12 digits -> the bounded observed space only (leading junk, trailing junk, or
    one of each). Generating every two-deletion combination would inflate the
    survivor count and make "exactly one" stop meaning anything (Codex).
    Anything else -> no candidates; we do not guess at an unseen defect shape.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == KING_PIN_DIGITS + 1:
        return sorted({digits[:i] + digits[i + 1:] for i in range(len(digits))})
    if len(digits) == KING_PIN_DIGITS + 2:
        return sorted({digits[:KING_PIN_DIGITS], digits[2:], digits[1:-1]})
    return []


def candidates_are_exhaustive(raw: str | None) -> bool:
    """True when candidate_pins() enumerated EVERY way ``raw`` could reduce to a PIN.

    Only the 11-digit case is exhaustive (every single deletion). The 12-digit set
    is a bounded guess at the observed defect shape, so a lone survivor there is
    NOT proof — the real parcel could need two interior deletions the set never
    generated, while the truncation parcel sits in GIS looking like a clean
    singleton (Codex P1). A non-exhaustive space therefore always needs owner
    corroboration.
    """
    return len(re.sub(r"\D", "", raw or "")) == KING_PIN_DIGITS + 1


def _normalize_token(tok: str) -> str:
    return re.sub(r"[^A-Z]", "", tok.upper())


# A hyphen joins a compound surname, and the two sources disagree about it:
# "SMITH-JONES MARY" vs "SMITH JONES MARY" are the same person, so split on it and
# let the sequence comparison line them up (Codex P2).
_SPLIT_TOKEN_RE = re.compile(r"[-\u2010-\u2015/]+")


def person_tokens(name: str | None) -> list[tuple[str, ...]]:
    """One normalised token tuple per PERSON named in ``name``.

    Both King's recorder and its assessor index a person as "LAST FIRST [MIDDLE]"
    with no comma; a comma form ("SMITH, JANE A") is normalised to the same order.
    Co-owners are split on + / & AND. A segment naming an organisation contributes
    nothing, and so does one with fewer than two usable tokens.

    Deliberately keeps the WHOLE sequence rather than a (surname, given) pair: a
    two-token surname makes a pair ambiguous — "VAN DYKE MARY" and "VAN DYKE JOHN"
    both reduce to ("VAN", "DYKE"), so a pair-based rule matches two DIFFERENT
    people and can hand a lead its neighbour's parcel (Codex P1).
    """
    out: list[tuple[str, ...]] = []
    if not name or not name.strip():
        return out
    for segment in _COOWNER_SPLIT_RE.split(name):
        seg = segment.strip()
        if not seg or _ENTITY_RE.search(seg):
            continue
        if "," in seg:
            last, _, rest = seg.partition(",")
            raw_tokens = [last] + rest.split()
        else:
            raw_tokens = seg.split()
        raw_tokens = [part for tok in raw_tokens for part in _SPLIT_TOKEN_RE.split(tok)]
        tokens = tuple(
            t for t in (_normalize_token(x) for x in raw_tokens) if t and t not in _SUFFIXES
        )
        if len(tokens) >= 2:
            out.append(tokens)
    return out


def _token_agrees(a: str, b: str) -> bool:
    """Equal, or one is the single-letter initial of the other."""
    if a == b:
        return True
    if len(a) == 1 and b.startswith(a):
        return True
    return len(b) == 1 and a.startswith(b)


def names_agree(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when two token sequences name the SAME person.

    Every position they share must agree and they must share at least two, so a
    longer name may add a middle name the other omits ("REINKE NORMAN L" agrees
    with "REINKE NORMAN LEONARD") while a differing given name never agrees
    ("VAN DYKE MARY" vs "VAN DYKE JOHN") and neither does a differing surname
    ("ROINSTAD ERIC R" vs "RONSTAD ERIC R" — a fuzzy spelling is not evidence).

    The FIRST TWO positions must additionally be spelled out and exactly equal.
    An initial may only ever corroborate a later position: "REINKE N L" would
    otherwise agree with "REINKE NORMAN LEONARD" on initials alone, which Codex
    rejects — initials are far too weak to decide which property a lead points at,
    and they are exactly what King's "+CO-OWNER" halves ("SHARYN R") look like.
    """
    n = min(len(a), len(b))
    if n < 2:
        return False
    if any(len(a[i]) == 1 or len(b[i]) == 1 or a[i] != b[i] for i in range(2)):
        return False
    return all(_token_agrees(a[i], b[i]) for i in range(2, n))


def owner_matches_party(owner: str | None, party: str | None) -> bool:
    """True when a person named in ``owner`` is a person named in ``party``.

    This is the tie-breaker that decides WHICH property a lead points at, so it
    compares whole names: surname-only, first-initial-only and fuzzy spellings are
    all rejected. A middle name or initial may differ, nothing else may.
    """
    return any(
        names_agree(o, p) for o in person_tokens(owner) for p in person_tokens(party)
    )


@dataclass
class _Attempt:
    """Counters for the ops view — every abort reason is distinguishable."""

    stats: dict = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1


def resolve_king_parcel(
    source_pid: str,
    party_name: str | None,
    *,
    gis_exists,
    owner_of,
    stats: dict | None = None,
) -> ResolvedParcel | None:
    """Recover the real King PIN behind a malformed recorder PID, or None.

    ``gis_exists(candidates) -> set[str]`` returns the subset that King's strict
    parcel layer actually carries. ``owner_of(pin) -> str | None`` returns the
    assessor's owner for a candidate (and must itself be echo-verified, so a
    candidate whose page names a different parcel yields None).

    Returns None whenever the evidence is not conclusive — a lead with no address
    is honest; a lead with a neighbour's address is not.
    """
    at = _Attempt(stats if stats is not None else {})
    at.bump("attempted")

    if is_well_formed_king_pin(source_pid):
        at.bump("skipped_well_formed")
        return None

    candidates = candidate_pins(source_pid)
    if not candidates:
        at.bump("no_candidates")
        return None

    present = sorted(gis_exists(candidates))
    if not present:
        # Also what a GIS outage looks like. Aborting on zero is what makes a
        # transient failure indistinguishable-but-safe: it can only ever cost us
        # a repair, never buy a wrong one.
        at.bump("no_candidate_in_gis")
        return None

    if len(present) == 1 and candidates_are_exhaustive(source_pid):
        at.bump("resolved_gis_singleton")
        return ResolvedParcel(
            parcel_id=present[0],
            method="gis_singleton",
            candidates_generated=len(candidates),
            candidates_in_gis=tuple(present),
        )

    # Either several real parcels survive, or one survived out of a candidate
    # space that was not exhaustive. Both need the same evidence: an owner/party
    # person match, and only if it picks exactly one.
    matches = []
    for pin in present:
        owner = owner_of(pin)
        if owner and owner_matches_party(owner, party_name):
            matches.append((pin, owner))
    if len(matches) != 1:
        at.bump("ambiguous_no_unique_owner_match" if matches else "multi_survivor_no_owner_match")
        _logger.info(
            "King parcel repair: %s left %d real candidates and %d owner matches — abstaining",
            source_pid, len(present), len(matches),
        )
        return None

    pin, owner = matches[0]
    at.bump("resolved_owner_match")
    return ResolvedParcel(
        parcel_id=pin,
        method="gis_plus_owner_match",
        candidates_generated=len(candidates),
        candidates_in_gis=tuple(present),
        matched_owner=owner,
        matched_party=party_name,
    )
