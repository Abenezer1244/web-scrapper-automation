"""NTS → lead matcher: decide which pre_foreclosure Result an NTS notice belongs to.

Pure scoring (no DB) so the false-match risk lives in one exhaustively-tested
place. The design (Codex consult): wrong auction data on a lead is worse than
missing auction data, so we only auto-attach on a HIGH-confidence, UNAMBIGUOUS
match — parcel exact, or normalized-address agreement backed by a borrower-name
signal. Address-only or name-only never auto-matches.

Confidence scale (Codex):
  parcel exact + address agrees + grantor agrees  0.99
  parcel exact + address agrees                   0.97
  parcel exact + grantor agrees                   0.96
  parcel exact alone                              0.90
  parcel BRIDGED + address agrees + grantor agrees 0.95  (see parcel_relation)
  parcel BRIDGED, anything less                   0.00
  address exact + grantor agrees                  0.92
  address exact alone                             0.80  (below threshold)
  grantor only / nothing                          0.00
Auto-attach threshold = 0.90, AND the best candidate must be unambiguous (no other
candidate also at/above threshold). Otherwise skip and log — never guess.
"""
from __future__ import annotations

import re
from typing import Any

from src.utils.address_intel import address_match_key

MATCH_THRESHOLD = 0.90


def _norm_parcel(parcel: Any) -> str:
    """Comparable parcel key: alphanumerics only, uppercased (strip hyphens/spaces/dots).

    Coerces to str defensively (a non-str parcel must not raise — Codex)."""
    if not parcel:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(parcel)).upper()


# ── Parcel identity as an explicit RELATION, not a bool pair.
#
# A county can publish the SAME property under two different identifiers. King does:
# the recorder index emits the 10-digit PIN (``2895650150``) while the trustee prints
# the 12-digit tax ACCOUNT number (``289565-0150-04``) on the Notice of Trustee Sale.
# Normalizing away the punctuation leaves the LENGTH difference, so those two read as
# a parcel CONFLICT and score 0.0 — a hard veto that an agreeing street address and an
# agreeing surname cannot override. Measured in prod 2026-09-19: 14 of 38 King notices
# carry the 12-digit form, and 7 notice/lead pairs were being vetoed this way, two of
# them live upcoming auctions.
#
# Four states rather than exact/conflict, because BRIDGED must score DIFFERENTLY from
# both (Codex P1): it is an inference about the county's ENCODING, not an observed
# identity, so it must never inherit the bare-exact 0.90 branch — that value sits
# exactly ON MATCH_THRESHOLD, which would auto-attach an uncorroborated bridge.
PARCEL_EXACT = "exact"
PARCEL_BRIDGED = "bridged"
PARCEL_CONFLICT = "conflict"
PARCEL_UNKNOWN = "unknown"      # one or both sides carry no parcel

# Counties where the PIN/account bridge applies. Gated by COUNTY, not by shape alone
# (Codex P1): "10 digits vs 12 digits sharing a prefix" is a King encoding rule, not a
# universal parcel-identity rule, and another county could legitimately have unrelated
# identifiers in that shape. An unknown or missing county never bridges — fail closed.
_PIN_ACCOUNT_BRIDGE_COUNTIES = frozenset({"king"})
_PIN_LEN = 10
_ACCOUNT_LEN = 12


def _canon_county(county: Any) -> str:
    """Lowercased, whitespace-stripped county slug; '' when absent."""
    return str(county).strip().lower() if county else ""


def _is_pin_account_pair(a: str, b: str) -> bool:
    """True when {a, b} is a 10-digit PIN and the 12-digit tax account extending it.

    Both already normalized. Deliberately ASYMMETRIC in length: 12-vs-12 sharing a
    10-digit prefix is two DIFFERENT account numbers and must stay a conflict, as must
    any other length pair or any non-digit parcel.
    """
    if len(a) == _ACCOUNT_LEN and len(b) == _PIN_LEN:
        a, b = b, a  # canonicalize to (pin, account)
    if len(a) != _PIN_LEN or len(b) != _ACCOUNT_LEN:
        return False
    if not (a.isdigit() and b.isdigit()):
        return False
    return b.startswith(a)


def parcel_relation(
    notice_parcel: Any, result_parcel: Any, *, county: Any = None
) -> str:
    """Classify two parcels as EXACT / BRIDGED / CONFLICT / UNKNOWN.

    The single source of truth for parcel identity. Used by BOTH ``score_match`` and
    ``best_match_group._same_property`` — patching only the scorer would let a bridged
    sibling read as a DIFFERENT property in the grouping step and bail the whole group
    to [], which is strictly worse than the veto it was meant to fix (Codex P1).
    """
    np_, rp = _norm_parcel(notice_parcel), _norm_parcel(result_parcel)
    if not np_ or not rp:
        return PARCEL_UNKNOWN
    if np_ == rp:
        return PARCEL_EXACT
    if _canon_county(county) in _PIN_ACCOUNT_BRIDGE_COUNTIES and _is_pin_account_pair(np_, rp):
        return PARCEL_BRIDGED
    return PARCEL_CONFLICT


def parcel_index_keys(parcel: Any, county: Any = None) -> set[str]:
    """Every key a parcel should be indexed/looked-up under in the candidate pool.

    The pool in ``nts_matcher_task`` is keyed on the NORMALIZED parcel, so a bridged
    candidate (different normalized string) would never enter the pool and the scorer
    would never see it. Indexing a bridging county's 12-digit account ALSO under its
    10-digit PIN makes pool construction independent of how the scorer happens to be
    tuned. Returns an empty set for an absent parcel.
    """
    np_ = _norm_parcel(parcel)
    if not np_:
        return set()
    keys = {np_}
    if (
        _canon_county(county) in _PIN_ACCOUNT_BRIDGE_COUNTIES
        and len(np_) == _ACCOUNT_LEN
        and np_.isdigit()
    ):
        keys.add(np_[:_PIN_LEN])
    return keys


def _surnames(name: Any) -> set[str]:
    """Uppercase alpha tokens length>=3 from a party/grantor name — a loose surname set.

    Borrower vs grantor strings come from different sources (recorder vs newspaper)
    in different orders ("SMITH JOHN" vs "JOHN AND JANE SMITH"), so we compare token
    SETS, not order. Drops short connectors (AND, JR) and non-alpha. Coerces to str
    defensively (Codex). Name agreement is a SECONDARY signal — it only lifts a
    score when parcel or address already agrees; it never auto-matches alone.
    """
    if not name:
        return set()
    toks = re.findall(r"[A-Za-z]{3,}", str(name).upper())
    stop = {"AND", "THE", "JR", "SR", "III", "HUSBAND", "WIFE", "TRUST", "ESTATE", "ETAL"}
    return {t for t in toks if t not in stop}


def _grantor_agrees(a: str | None, b: str | None) -> bool:
    """True when two name strings share a meaningful surname token."""
    sa, sb = _surnames(a), _surnames(b)
    return bool(sa and sb and (sa & sb))


def score_match(
    *,
    notice_parcel: str | None,
    notice_addr_key: str | None,
    notice_grantor: str | None,
    result_parcel: str | None,
    result_addr_key: str | None,
    result_party_name: str | None,
    county: Any = None,
) -> float:
    """Confidence (0.0–1.0) that the NTS notice describes the same property/owner.

    See module docstring for the scale. Callers pass `result_addr_key` already
    computed via address_match_key so notice + lead are normalized identically.

    `county` is the county BOTH sides belong to (matching is already scoped per county
    upstream). It only ever enables the PIN/account bridge; omitting it can never make
    a match MORE likely, so every existing caller keeps its exact behavior.
    """
    nk = notice_addr_key or ""
    rk = result_addr_key or ""

    relation = parcel_relation(notice_parcel, result_parcel, county=county)
    addr_exact = bool(nk and rk and nk == rk)
    grantor_ok = _grantor_agrees(notice_grantor, result_party_name)

    # Conflicting parcels (both present, different) = different property — do NOT
    # auto-match even if the address key + names coincide (Codex: two units at the
    # same street+zip with the same surname would otherwise reach 0.92). We favor a
    # missed match over a wrong one.
    if relation == PARCEL_CONFLICT:
        return 0.0

    # A BRIDGED parcel is an inference about King's PIN/account encoding, not an
    # observed identity, so it demands BOTH independent corroborators before it may
    # auto-attach — never the bare 0.90 an exact parcel earns on its own. Scored below
    # every corroborated exact match (0.96/0.97/0.99) and above the threshold, so a
    # true exact match always wins a tie for the same lead.
    if relation == PARCEL_BRIDGED:
        return 0.95 if (addr_exact and grantor_ok) else 0.0

    if relation == PARCEL_EXACT:
        if addr_exact and grantor_ok:
            return 0.99
        if addr_exact:
            return 0.97
        if grantor_ok:
            return 0.96
        return 0.90
    # No parcel conflict and no parcel match (>=1 parcel missing): lean on address.
    if addr_exact:
        return 0.92 if grantor_ok else 0.80
    return 0.0  # grantor-only or nothing never auto-matches


def best_match(
    notice: dict[str, Any], candidates: list[dict[str, Any]], *, county: Any = None
) -> tuple[Any, float] | None:
    """Pick the single unambiguous best lead for a notice, or None.

    `notice` carries parcel / property_address_normalized / grantor. Each candidate
    is a dict with `id`, `parcel`, `addr_key` (precomputed), `party_name`. Returns
    (candidate_id, confidence) only when the top score is >= MATCH_THRESHOLD AND no
    OTHER candidate also reaches the threshold (ambiguity = skip, never guess).
    """
    scored: list[tuple[Any, float]] = []
    for c in candidates:
        s = score_match(
            notice_parcel=notice.get("parcel"),
            notice_addr_key=notice.get("property_address_normalized"),
            notice_grantor=notice.get("grantor"),
            result_parcel=c.get("parcel"),
            result_addr_key=c.get("addr_key"),
            result_party_name=c.get("party_name"),
            county=county,
        )
        if s > 0:
            scored.append((c.get("id"), s))
    if not scored:
        return None
    scored.sort(key=lambda x: x[1], reverse=True)
    top_id, top = scored[0]
    if top < MATCH_THRESHOLD:
        return None
    # Ambiguity guard: a second candidate also at/above threshold = don't guess.
    if len(scored) > 1 and scored[1][1] >= MATCH_THRESHOLD:
        return None
    return (top_id, top)


def best_match_group(
    notice: dict[str, Any], candidates: list[dict[str, Any]], *, county: Any = None
) -> list[tuple[Any, float]]:
    """All Results for the SAME winning property that should receive this notice.

    Multi-tenant coverage: an NTS notice is PUBLIC statutory data about a
    PROPERTY, and several tenants can each hold a pre_foreclosure Result for the
    same foreclosed property. ``best_match`` returns a SINGLE id and bails on any
    second at-threshold candidate — which silently drops the (common, valuable)
    case where two tenants track the same property, since both score ≥ threshold.

    This returns EVERY at-threshold candidate that resolves to the same physical
    property as the top match (same normalized parcel or same address key), so the
    caller attaches the auction data to all of them. If a second candidate at/above
    threshold is a DIFFERENT property, that is genuine ambiguity → return [] (never
    guess across distinct properties), preserving ``best_match``'s safety contract.
    """
    scored: list[tuple[dict[str, Any], float]] = []
    for c in candidates:
        s = score_match(
            notice_parcel=notice.get("parcel"),
            notice_addr_key=notice.get("property_address_normalized"),
            notice_grantor=notice.get("grantor"),
            result_parcel=c.get("parcel"),
            result_addr_key=c.get("addr_key"),
            result_party_name=c.get("party_name"),
            county=county,
        )
        if s >= MATCH_THRESHOLD:
            scored.append((c, s))
    if not scored:
        return []
    scored.sort(key=lambda x: x[1], reverse=True)
    top_c = scored[0][0]
    win_parcel = _norm_parcel(top_c.get("parcel"))
    win_addr = top_c.get("addr_key") or ""

    def _same_property(c: dict[str, Any]) -> bool:
        cp = _norm_parcel(c.get("parcel"))
        # Parcel is authoritative in BOTH directions: if EITHER the winner or this
        # candidate carries a parcel, they are the same property ONLY when the two
        # parcels EXACTly agree or are the same parcel under the county's two
        # encodings (parcel_relation — the SAME predicate the scorer uses, so a
        # bridged sibling can never score at threshold and then be read here as a
        # different property, which would bail the whole group; Codex P1).
        # UNKNOWN (exactly one side has a parcel) stays "different", as before.
        # The address fallback is used ONLY when NEITHER side has a parcel —
        # addr_key strips unit numbers (two condo/apartment units share a base
        # street+ZIP key), so a parcel-less address must never group two distinct
        # units onto one notice (Codex P1: a false cross-property attach is worse
        # than a missed match).
        if win_parcel or cp:
            return parcel_relation(win_parcel, cp, county=county) in (
                PARCEL_EXACT, PARCEL_BRIDGED,
            )
        ca = c.get("addr_key") or ""
        return bool(win_addr and ca and ca == win_addr)

    group: list[tuple[dict[str, Any], float]] = []
    for c, s in scored:
        if _same_property(c):
            group.append((c, s))
        else:
            # A different property also reached the threshold — ambiguous, skip.
            return []

    # The bridge relation is NOT transitive (Codex P1): a 10-digit PIN bridges to
    # EVERY 12-digit account that extends it, but two such accounts do not bridge to
    # each other. So "every member is the same property as the WINNER" does not imply
    # the members agree among themselves, and which candidate happens to sort first
    # would decide the outcome. Fail closed on any conflicting pair inside the group.
    # (Prod 2026-09-19: zero King PINs carry two distinct 12-digit accounts, in either
    # nts_notices or results — this guards a hazard that has not fired yet.)
    for i, (a, _) in enumerate(group):
        for b, _ in group[i + 1:]:
            if parcel_relation(
                a.get("parcel"), b.get("parcel"), county=county
            ) == PARCEL_CONFLICT:
                return []

    return [(c.get("id"), s) for c, s in group]


def result_match_candidate(result: Any) -> dict[str, Any]:
    """Build a matcher candidate dict from a Result ORM row (precompute addr_key)."""
    def _get(name: str) -> Any:
        return result.get(name) if isinstance(result, dict) else getattr(result, name, None)

    return {
        "id": _get("id"),
        "parcel": _get("parcel_id"),
        "addr_key": address_match_key(_get("property_address")),
        "party_name": _get("party_name"),
    }
