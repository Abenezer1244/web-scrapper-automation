"""Pierce County probate parcel repair via legal description.

WHY: Pierce ARMS probate rows occasionally carry a parcel_id that does not
exist in the Pierce assessor GIS — a county-index typo in the plat prefix
(observed live: "6779000110" stored for legal "PARKWOOD LT 11" whose real
parcel is "6776000110" = 2322 BRYCE CANYON CT; plat prefix 6779 has ZERO
parcels countywide, only the 4-digit prefix is wrong, the lot suffix 000110
is correct). A bad parcel resolves to no address, so the lead ships with a
parcel but no mailing/property address.

This module recovers the property from the LEGAL DESCRIPTION, but ONLY under
tight guards (pressure-tested with Codex) so it can never mis-attach a
neighbouring property:

  1. Only run when the scraped parcel is a CONFIRMED hard-negative in Pierce
     GIS (a real 200/0-features response — not a transient timeout/error).
  2. Parse ONLY a simple "PLAT LT <n>" legal — reject block/division/addition/
     plural-lots/range/portion qualifiers we do not model (they can create
     false uniqueness).
  3. Query the plat by an escaped, metachar-free LIKE; treat a capped /
     exceededTransferLimit response as ambiguous.
  4. Post-filter to the EXACT singular lot token ("L 11" ≠ "L 110", ≠ "L 11-12")
     with a non-empty situs address, and require EXACTLY ONE survivor.
  5. Replace the parcel ONLY when the assessor match shares the lot suffix with
     the scraped parcel (differ only in the plat prefix — the confirmed defect
     class). Full provenance is stored in enrichment_data for audit.

Extended 2026-09-02 (Test 2 audit, Codex-reviewed) for two more live typo classes
seen on pre_foreclosure rows, under the SAME hard-negative + exact-legal guards:
  * "THORSON RIDGE LT 5" scraped 9066600050, real 9066000050 (one substituted
    digit); "RHODODENDRON LANES LT 6 BLK 3" scraped 718500090 (9 digits), real
    7185000190 (one dropped digit). So: a trailing "BLK m" is parsed and the
    candidate legal must carry the same bounded block token; and the parcel guard
    accepts a digit-string edit distance of exactly 1 — but ONLY when the legal
    filters already left EXACTLY ONE survivor (edit distance never chooses
    between neighbours; the 6-digit lot-suffix rule remains the only guard
    allowed to disambiguate several survivors).

Scope: Pierce/WA, probate + pre_foreclosure. HTTP calls go through safe_get (SSRF defense).
"""
from __future__ import annotations

import re

from src.scrapers.enrichment.county_gis import _KNOWN_GIS_ENDPOINTS
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.pierce_legal")

_PIERCE = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
_ENDPOINT = _PIERCE["endpoint"]

# ARMS more-parties marker ("PARKWOOD LT 11 (+)").
_PLUS = re.compile(r"\s*\(\+\)\s*")

# Subdivision qualifiers we do NOT model — their presence means the plat+lot
# pair is NOT a unique property identity, so we refuse to guess (Codex).
_UNMODELED = re.compile(
    r"\b(?:BLK|BLOCK|DIV|DIVISION|ADD|ADDITION|LESS|EXC|EXCEPT|POR|PORTION|"
    r"TRACT|TR|UNIT|PARCEL|SEC|SECTION|LOTS|LTS|THRU|THROUGH|TO|AND)\b"
    r"|\d\s*[-&/,]\s*\d",  # multi-lot list/range: 11-12, 11 & 12, 11/12, 11,12
    re.IGNORECASE,
)

# A SINGULAR lot: "LOT 11" / "LT 11" / "L 11" / "L#11" / "L 011", capturing the
# number. Used to locate the lot in the SCRAPED legal (which we already vetted
# has no plural/range qualifier via _UNMODELED).
_LOT_IN_SCRAPE = re.compile(r"\bL(?:OT|T)?\s*#?\s*0*(\d{1,4})\b", re.IGNORECASE)

# LIKE metacharacters that would widen an ArcGIS where-clause if unescaped.
_LIKE_META = re.compile(r"[%_]")

# A single trailing block qualifier right after the lot ("... LT 6 BLK 3"). Only
# this exact position is modelled; a block anywhere else is still rejected by
# _UNMODELED. Bounded so "BLK 30" can never parse as block 3.
_TRAILING_BLOCK = re.compile(r"\s+B(?:LK|LOCK)?\s*#?\s*0*(\d{1,4})\s*$", re.IGNORECASE)


def parse_pierce_legal(legal: str | None) -> tuple[str, str, str | None] | None:
    """Parse "PARKWOOD LT 11 (+)" -> ("PARKWOOD", "11", None);
    "RHODODENDRON LANES LT 6 BLK 3 (+)" -> ("RHODODENDRON LANES", "6", "3").

    Returns (plat, lot, block) for a SIMPLE single-lot platted legal (block is
    None when absent), or None when the legal is empty, has no lot, carries an
    unmodeled qualifier, or the plat name is unusable (too short / contains a
    LIKE metacharacter).
    """
    if not legal:
        return None
    text = _PLUS.sub(" ", legal).strip().upper()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None

    block: str | None = None
    bm = _TRAILING_BLOCK.search(text)
    if bm:
        block = bm.group(1).lstrip("0") or "0"
        text = text[: bm.start()].strip()

    m = _LOT_IN_SCRAPE.search(text)
    if not m:
        return None
    plat = text[: m.start()].strip(" ,:-")
    lot = m.group(1).lstrip("0") or "0"

    # Reject anything we do not model on EITHER side of the lot token, so a
    # "PARKWOOD DIV 3 LT 11" or "PARKWOOD LTS 11-12" never reduces to (PARKWOOD, 11).
    # (The one trailing block clause was already split off above; any OTHER block
    # placement is still caught here.)
    if _UNMODELED.search(text):
        return None
    if len(plat) < 3 or _LIKE_META.search(plat) or not any(c.isalpha() for c in plat):
        return None
    return plat, lot, block


def _lot_token_re(lot: str) -> re.Pattern:
    """Exact singular-lot matcher for a candidate GIS legal: matches "L 11" /
    "LT 11" / "LOT 011" but NOT "L 110" (different lot) nor "L 11-12" (range)."""
    return re.compile(
        rf"\bL(?:OT|T)?\s*#?\s*0*{re.escape(lot)}\b(?!\s*[-&/,]\s*\d)",
        re.IGNORECASE,
    )


def _block_token_re(block: str) -> re.Pattern:
    """Exact bounded block matcher for a candidate GIS legal: "B 3" / "BLK 3" /
    "BLOCK 03" but NOT "B 30" nor "B 3-4" (Codex: bounded tokens, never substrings)."""
    return re.compile(
        rf"\bB(?:LK|LOCK)?\s*#?\s*0*{re.escape(block)}\b(?!\s*[-&/,]\s*\d)",
        re.IGNORECASE,
    )


def collect_legal_matches(
    features: list[dict], plat: str, lot: str, block: str | None = None
) -> list[dict]:
    """From raw ArcGIS features, keep every candidate whose Legal_Description
    contains the plat AND the exact singular lot token AND (when the scraped
    legal names one) the exact block token AND a non-empty situs address.
    Returns a list (a bare plat+lot can hit multiple subdivisions — e.g.
    PARKWOOD / PARKWOOD DIV 2 / DIV 3 each have a lot 11); the caller
    disambiguates by the scraped parcel's lot suffix.
    """
    tok = _lot_token_re(lot)
    btok = _block_token_re(block) if block else None
    plat_up = plat.upper()
    out: list[dict] = []
    for f in features or []:
        attrs = f.get("attributes") or {}
        legal = (attrs.get("Legal_Description") or "").upper()
        site = (attrs.get("Site_Address") or "").replace("&nbsp;", "").strip()
        if not site or plat_up not in legal or not tok.search(legal):
            continue
        if btok is not None and not btok.search(legal):
            continue
        # NB: do NOT apply _UNMODELED here — every Pierce GIS legal begins with
        # "Section .. Township .. Range .. Quarter ..", so it would reject all
        # candidates. Multi-lot / range candidates are already excluded by the
        # exact-lot token's negative lookahead ("L 11-12", "L 11 & 12", "LOTS 11").
        mail_parts = [str(attrs.get(k)).strip()
                      for k in ("Delivery_Address", "City_State", "Zipcode")
                      if attrs.get(k) and str(attrs.get(k)).strip()]
        out.append({
            "parcel_id": attrs.get("TaxParcelNumber"),
            "property_address": site,
            "mailing_address": ", ".join(mail_parts) if mail_parts else None,
            "gis_legal_description": attrs.get("Legal_Description"),
        })
    return out


def same_lot_suffix(scraped: str | None, matched: str | None) -> bool:
    """True when two 10-digit Pierce parcels share the 6-digit lot suffix and
    differ only in the 4-digit plat prefix — the confirmed typo class."""
    a = re.sub(r"\D", "", scraped or "")
    b = re.sub(r"\D", "", matched or "")
    if len(a) != 10 or len(b) != 10:
        return False
    return a[4:] == b[4:] and a[:4] != b[:4]


def _digit_edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two digit strings (small inputs, O(n*m))."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def legal_plat_adjacent(gis_legal: str | None, plat: str, lot: str, block: str | None = None) -> bool:
    """True when the GIS legal names the plat IMMEDIATELY followed by the lot token
    (and the block token, when given): "… RHODODENDRON LANES L 6 B 3 …",
    "THORSON RIDGE: THORSON RIDGE L 5 …". A subdivision qualifier between plat and
    lot ("PARKWOOD DIV 2 L 11") fails — so the edit-distance path, which has no
    lot-suffix anchor, can never adopt a neighbouring division's lot (Codex)."""
    if not gis_legal:
        return False
    legal = re.sub(r"\s+", " ", gis_legal.upper())
    pat = rf"\b{re.escape(plat.upper())}\s*:?\s+L(?:OT|T)?\s*#?\s*0*{re.escape(lot)}\b(?!\s*[-&/,]\s*\d)"
    if block:
        pat += rf"\s+B(?:LK|LOCK)?\s*#?\s*0*{re.escape(block)}\b(?!\s*[-&/,]\s*\d)"
    return re.search(pat, legal) is not None


def parcel_repair_method(scraped: str | None, matched: str | None) -> str | None:
    """Which guard (if any) lets ``matched`` replace ``scraped``.

    "plat_lot_unique_suffix" — same 6-digit lot suffix, different 4-digit plat
    prefix (the original confirmed class; also the ONLY rule allowed to pick one
    of several legal survivors). "plat_lot_unique_edit1" — the digit strings
    differ by exactly one substitution / insertion / deletion (recorder typo or
    dropped digit); the caller must only use this when the legal filters left a
    SINGLE survivor. None otherwise (never replace).
    """
    if same_lot_suffix(scraped, matched):
        return "plat_lot_unique_suffix"
    a = re.sub(r"\D", "", scraped or "")
    b = re.sub(r"\D", "", matched or "")
    if len(b) != 10 or not (6 <= len(a) <= 10) or a == b:
        return None
    if _digit_edit_distance(a, b) == 1:
        return "plat_lot_unique_edit1"
    return None


def parcel_hard_negative(parcel_id: str | None) -> bool:
    """True ONLY on a definitive Pierce GIS 200/0-features response for this
    parcel (i.e. the parcel genuinely does not exist). False if it resolves;
    False on any error/timeout (never treat a transient failure as 'absent')."""
    # Digits-only: the parcel is external scraped data going into an ArcGIS
    # where-clause, so strip everything non-numeric — no quote/metachar can
    # survive to break out of TaxParcelNumber='<apn>' (Codex).
    apn = re.sub(r"\D", "", parcel_id or "")
    if len(apn) < 6:
        return False
    try:
        resp = safe_get(
            _ENDPOINT,
            params={"where": f"{_PIERCE['parcel_field']}='{apn}'",
                    "outFields": _PIERCE["parcel_field"], "returnGeometry": "false",
                    "f": "json", "resultRecordCount": 1},
            require_allowlisted=False, timeout=15,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        if "error" in data:
            return False
        return len(data.get("features") or []) == 0
    except Exception as exc:
        _logger.warning("Pierce parcel existence check failed for %r: %s", apn, str(exc)[:80])
        return False


def find_pierce_parcels_by_legal(legal: str | None) -> list[dict]:
    """Return ALL assessor properties matching a simple Pierce plat+lot legal
    (each: {parcel_id, property_address, mailing_address, gis_legal_description}).
    A bare plat+lot may hit several subdivisions; the caller disambiguates by the
    scraped parcel's lot suffix. Returns [] on parse/query failure or an ambiguous
    (server-capped) response. Logs candidate/survivor counts (never silent)."""
    parsed = parse_pierce_legal(legal)
    if not parsed:
        return []
    plat, lot, block = parsed
    like_plat = plat.replace("'", "''")  # metachars already rejected in parse
    # Pull the whole plat in one request: the Pierce FeatureServer maxRecordCount
    # is 2000, so a plat under that returns complete (exceededTransferLimit=false)
    # and the exact-lot post-filter can prove uniqueness. A plat exceeding 2000
    # rows caps out -> treated as ambiguous -> None (safe).
    cap = 2000
    try:
        resp = safe_get(
            _ENDPOINT,
            params={"where": f"UPPER(Legal_Description) LIKE '%{like_plat}%'",
                    "outFields": ("TaxParcelNumber,Site_Address,Delivery_Address,"
                                  "City_State,Zipcode,Legal_Description"),
                    "returnGeometry": "false", "f": "json", "resultRecordCount": cap},
            require_allowlisted=False, timeout=20,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if "error" in data:
            _logger.warning("Pierce legal query error for plat %r: %s", plat, str(data["error"])[:120])
            return []
        feats = data.get("features") or []
        # A capped / transfer-limited response means we cannot see the full plat,
        # so we cannot prove the lot-suffix pick is exhaustive -> refuse (safe).
        if data.get("exceededTransferLimit") or len(feats) >= cap:
            _logger.info("Pierce legal %r/%s ambiguous: server capped (%d rows)", plat, lot, len(feats))
            return []
        matches = collect_legal_matches(feats, plat, lot, block)
        _logger.info("Pierce legal %r LT %s BLK %s: %d plat rows, %d exact-lot survivors",
                     plat, lot, block, len(feats), len(matches))
        return matches
    except Exception as exc:
        _logger.warning("Pierce legal lookup failed for %r: %s", plat, str(exc)[:80])
        return []
