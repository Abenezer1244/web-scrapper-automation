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
) -> float:
    """Confidence (0.0–1.0) that the NTS notice describes the same property/owner.

    See module docstring for the scale. Callers pass `result_addr_key` already
    computed via address_match_key so notice + lead are normalized identically.
    """
    np_, nk = _norm_parcel(notice_parcel), (notice_addr_key or "")
    rp, rk = _norm_parcel(result_parcel), (result_addr_key or "")

    parcel_exact = bool(np_ and rp and np_ == rp)
    parcel_conflict = bool(np_ and rp and np_ != rp)
    addr_exact = bool(nk and rk and nk == rk)
    grantor_ok = _grantor_agrees(notice_grantor, result_party_name)

    # Conflicting parcels (both present, different) = different property — do NOT
    # auto-match even if the address key + names coincide (Codex: two units at the
    # same street+zip with the same surname would otherwise reach 0.92). We favor a
    # missed match over a wrong one. (A same-property parcel-format drift across
    # sources falls here too and is left for manual/other signals — the safe side.)
    if parcel_conflict:
        return 0.0

    if parcel_exact:
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


def best_match(notice: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[Any, float] | None:
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
    notice: dict[str, Any], candidates: list[dict[str, Any]]
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
        # Parcel is authoritative in BOTH directions: if EITHER the winner or
        # this candidate carries a parcel, they are the same property ONLY when
        # both parcels are present and equal. The address fallback is used ONLY
        # when NEITHER side has a parcel — addr_key strips unit numbers (two
        # condo/apartment units share a base street+ZIP key), so a parcel-less
        # address must never group two distinct units onto one notice (Codex P1:
        # a false cross-property attach is worse than a missed match).
        if win_parcel or cp:
            return bool(win_parcel and cp and cp == win_parcel)
        ca = c.get("addr_key") or ""
        return bool(win_addr and ca and ca == win_addr)

    group: list[tuple[Any, float]] = []
    for c, s in scored:
        if _same_property(c):
            group.append((c.get("id"), s))
        else:
            # A different property also reached the threshold — ambiguous, skip.
            return []
    return group


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
