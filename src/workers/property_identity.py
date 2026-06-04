"""Canonical property identity for cross-list overlap (Phase 1).

A property's identity for the "on both lists" feature is its normalized
parcel/address, hashed. This MUST stay in lockstep with the *strong* branch
of `_compute_dedup_hash` in src/workers/tasks.py — that function imports the
helpers below so the two cannot drift.

Weak (name+date) identities are intentionally NOT representable here:
different record-type lists key the same property differently by name, so a
name/date match is unsafe for cross-list overlap. `compute_property_key`
returns None for weak rows, and they are excluded from membership.
"""
import hashlib
import re


def normalize_parcel(parcel_id: str | None) -> str:
    return (parcel_id or "").strip().upper().replace("-", "").replace(" ", "")


def normalize_address(property_address: str | None) -> str:
    addr = (property_address or "").strip().upper()
    addr = re.sub(r"[\.,#]", " ", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def is_strong_identity(parcel_id: str | None, property_address: str | None) -> bool:
    """True when parcel OR address is specific enough to identify a property.

    Mirrors the parcel_ok/addr_ok thresholds in _compute_dedup_hash.
    """
    parcel = normalize_parcel(parcel_id)
    addr = normalize_address(property_address)
    parcel_ok = len(parcel) >= 4 and any(c.isdigit() for c in parcel)
    addr_ok = len(addr) >= 8 and any(c.isalpha() for c in addr)
    return parcel_ok or addr_ok


def compute_property_key(parcel_id: str | None, property_address: str | None) -> str | None:
    """sha256 of normalized `parcel|address`, or None for weak identity.

    Equal to the strong-branch key of _compute_dedup_hash for the same inputs.
    """
    if not is_strong_identity(parcel_id, property_address):
        return None
    key = f"{normalize_parcel(parcel_id)}|{normalize_address(property_address)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
