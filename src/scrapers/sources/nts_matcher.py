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


def _norm_parcel(parcel: str | None) -> str:
    """Comparable parcel key: alphanumerics only, uppercased (strip hyphens/spaces/dots)."""
    if not parcel:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", parcel).upper()


def _surnames(name: str | None) -> set[str]:
    """Uppercase alpha tokens length>=3 from a party/grantor name — a loose surname set.

    Borrower vs grantor strings come from different sources (recorder vs newspaper)
    in different orders ("SMITH JOHN" vs "JOHN AND JANE SMITH"), so we compare token
    SETS, not order. Drops short connectors (AND, JR) and non-alpha.
    """
    if not name:
        return set()
    toks = re.findall(r"[A-Za-z]{3,}", name.upper())
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
    addr_exact = bool(nk and rk and nk == rk)
    grantor_ok = _grantor_agrees(notice_grantor, result_party_name)

    if parcel_exact:
        if addr_exact and grantor_ok:
            return 0.99
        if addr_exact:
            return 0.97
        if grantor_ok:
            return 0.96
        return 0.90
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
