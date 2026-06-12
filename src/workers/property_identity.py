"""Canonical property identity for cross-list overlap (Phase 1; re-keyed 2026-06-12).

TWO DISTINCT KEYS LIVE HERE — they deliberately DIVERGED on 2026-06-12:

1. ``compute_property_key`` — the OVERLAP/IDENTITY key (results.property_key +
   property_list_membership). Parcel-PRIMARY and county/state-scoped: the parcel
   alone decides identity when present, because different pipelines store
   different address strings for the SAME parcel (tax scrapers keep the situs
   city+ZIP4; GIS enrichment writes street-only), and hashing ``parcel|address``
   together made identical parcels diverge — tax lists could NEVER overlap
   recorder lists (live bug, 2026-06-12). County/state scoping prevents the
   converse failure: the same parcel NUMBER exists in many counties, so a bare
   parcel hash would create false overlap across counties. Branch prefixes
   (``P|`` / ``A|``) keep a parcel string from ever colliding with an address.

2. ``legacy_strong_signature`` — the ORIGINAL ``parcel|address`` hash. This is
   the strong branch of ``_compute_dedup_hash`` in src/workers/tasks.py, which
   keys ``delivered_records`` — the BILLING dedup. It must stay byte-identical
   forever: changing it would make every already-delivered lead re-read as new
   (mass re-billing + duplicate deliveries). The enrichment-reuse gate also
   compares against it (reuse requires dedup_hash == this signature). DO NOT
   "unify" these two functions; the old lockstep comment is dead on purpose.

Weak (name+date) identities are intentionally NOT representable by either:
different record-type lists key the same property differently by name, so a
name/date match is unsafe for cross-list overlap. Both return None for weak
rows, and they are excluded from membership.

Changing ``compute_property_key``'s scheme requires re-running
``scripts/backfill_property_keys.py`` (recomputes results.property_key and
rebuilds property_list_membership) — stored keys do not self-heal.
"""
import hashlib
import re


def normalize_parcel(parcel_id: str | None) -> str:
    # NOTE: leading zeros are KEPT deliberately (Codex 2026-06-12): stripping
    # them could merge DISTINCT parcels, which is worse than missing an overlap.
    # If a specific county's recorder provably drops zeros vs its treasurer,
    # add a county-specific normalizer — never a global lstrip.
    return (parcel_id or "").strip().upper().replace("-", "").replace(" ", "")


def normalize_address(property_address: str | None) -> str:
    addr = (property_address or "").strip().upper()
    addr = re.sub(r"[\.,#]", " ", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def is_strong_identity(parcel_id: str | None, property_address: str | None) -> bool:
    """True when parcel OR address is specific enough to identify a property.

    Mirrors the parcel_ok/addr_ok thresholds in legacy_strong_signature.
    """
    parcel = normalize_parcel(parcel_id)
    addr = normalize_address(property_address)
    parcel_ok = len(parcel) >= 4 and any(c.isdigit() for c in parcel)
    addr_ok = len(addr) >= 8 and any(c.isalpha() for c in addr)
    return parcel_ok or addr_ok


def legacy_strong_signature(
    parcel_id: str | None, property_address: str | None
) -> str | None:
    """The ORIGINAL strong-identity hash: sha256 of ``parcel|address``.

    FROZEN: this is the strong branch of the billing dedup_hash
    (delivered_records) and the enrichment-reuse gate. Byte-identical to the
    pre-2026-06-12 compute_property_key. Never change this formula.
    """
    if not is_strong_identity(parcel_id, property_address):
        return None
    key = f"{normalize_parcel(parcel_id)}|{normalize_address(property_address)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def compute_property_key(
    parcel_id: str | None,
    property_address: str | None,
    county: str | None,
    state: str | None,
) -> str | None:
    """The OVERLAP identity key: parcel-primary, county/state-scoped.

    - Parcel branch (a usable parcel exists): the parcel ALONE identifies the
      property within its county — the address is ignored so format drift
      between pipelines (city+ZIP4 situs vs street-only enrichment) cannot
      split identity.
    - Address branch (no usable parcel): the normalized address, county-scoped
      so a street-only address can't collide across cities/counties.
    - Weak identity: None (excluded from membership/overlap).

    ``county``/``state`` are REQUIRED call-through context (every caller has
    the ScraperConfig in scope); normalized here so casing can't split keys.
    """
    parcel = normalize_parcel(parcel_id)
    addr = normalize_address(property_address)
    parcel_ok = len(parcel) >= 4 and any(c.isdigit() for c in parcel)
    addr_ok = len(addr) >= 8 and any(c.isalpha() for c in addr)
    scope = f"{(state or '').strip().upper()}|{(county or '').strip().lower()}"
    if parcel_ok:
        key = f"P|{scope}|{parcel}"
    elif addr_ok:
        key = f"A|{scope}|{addr}"
    else:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
