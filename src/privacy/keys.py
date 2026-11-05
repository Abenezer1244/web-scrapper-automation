"""Versioned keyed HMACs for privacy suppression rules (design section 5).

A rule names a property and a person without storing either: it stores
``privacy_hmac(kind, value)`` digests. Names and addresses in public records are
guessable, so an unkeyed hash would be reversible by trying candidates; a keyed
HMAC is not, to anyone without the key.

Unlike the email blind index this keyring ROTATES. Every digest is tied to a key
version, a rule records the version it was made under, and during a rotation a
rule carries digests under every live version (``privacy_hmacs``), so matching
never has a gap. Each version's key is HKDF-derived from its configured secret
with the version in the context, so two versions can never collide even if an
operator reuses a secret.

Domain separation: the ``kind`` ("property", "mailing", "person", "entity",
"phone", "email") is inside the MAC input, so a value of one kind can never match
a rule of another (a surname equal to a street name, say).

Canonical values only: ``privacy_hmac`` refuses a value that is not already in
its kind's canonical form (the normalisers in src/privacy/subjects.py), because a
digest of 'Smith|John' and one of 'smith|john' would never meet. Property and
mailing addresses have NO canonical form until the address normaliser ships with
the address matcher, so until then they cannot be keyed at all: a weak floor
('123 Main St.' vs '123 MAIN STREET') would make rules that silently never match.

Configuration is read ONCE per process, as one snapshot of (current version,
keyring): a running worker never mixes a current version from new settings with a
keyring from old ones. A rotation therefore takes effect at process restart, which
is how every other secret here is deployed. ``reset_keyring_cache`` exists for tests.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.config import settings
from src.privacy.subjects import (
    email_key_value,
    entity_key_value,
    person_key_value,
    phone_key_value,
)

KINDS = frozenset({"property", "mailing", "person", "entity", "phone", "email"})
_VERSION_RE = re.compile(r"^v[1-9][0-9]{0,3}$")
_INFO_PREFIX = b"bridgeleads:privacy-hmac:"
_DEV_INFO_PREFIX = b"bridgeleads:privacy-hmac:dev:"
_MIN_SECRET_BYTES = 32
# The SECRET_KEY-derived fallback is allowed ONLY here. An allowlist, not a
# "!= production" test: a misspelt or new environment name ("prod", "staging")
# must fail closed, not quietly key real rules with a rotating JWT secret.
_FALLBACK_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


def _canonical_person(value: str) -> str | None:
    surname, sep, given = value.partition("|")
    return person_key_value(surname, given) if sep else None


def _no_canonical_form_yet(value: str) -> str | None:
    return None  # refused until the address normaliser defines the form


# kind -> the normaliser whose output is that kind's only acceptable input.
_CANONICAL = {
    "person": _canonical_person,
    "entity": entity_key_value,
    "phone": phone_key_value,
    "email": email_key_value,
    "property": _no_canonical_form_yet,
    "mailing": _no_canonical_form_yet,
}

_lock = threading.Lock()
_snapshot: tuple[str, dict[str, bytes]] | None = None


def _derive(secret: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret)


def _load_keyring() -> tuple[str, dict[str, bytes]]:
    """Parse the settings into (current version, {version: derived key}).

    Raises on any defect. Fails CLOSED when PRIVACY_HMAC_KEYS is unset unless the
    environment is a listed development/test one AND PII strict mode is off (strict
    mode means "behave like production" for every PII control): a SECRET_KEY-derived
    key would change with a routine JWT-key rotation and silently unmatch every
    suppression rule, which would re-expose people who asked to be removed.
    """
    configured = (settings.PRIVACY_HMAC_KEYS or "").strip()
    current = (settings.PRIVACY_HMAC_CURRENT_VERSION or "").strip()
    if not _VERSION_RE.match(current):
        raise RuntimeError(f"PRIVACY_HMAC_CURRENT_VERSION {current!r} is not like 'v1'")
    if not configured:
        environment = (settings.ENVIRONMENT or "").strip().lower()
        if environment not in _FALLBACK_ENVIRONMENTS or settings.PII_ENCRYPTION_STRICT:
            raise RuntimeError(
                f"PRIVACY_HMAC_KEYS is not set (ENVIRONMENT={environment!r}, "
                f"PII_ENCRYPTION_STRICT={settings.PII_ENCRYPTION_STRICT}). The SECRET_KEY-"
                "derived fallback is for development/test without strict mode only: a "
                "SECRET_KEY rotation would silently unmatch every privacy suppression rule."
            )
        return current, {current: _derive(settings.SECRET_KEY.encode("utf-8"),
                                          _DEV_INFO_PREFIX + current.encode("ascii"))}
    ring: dict[str, bytes] = {}
    for entry in configured.split(","):
        version, sep, secret = entry.strip().partition(":")
        secret_bytes = secret.strip().encode("utf-8")
        # Bytes, not characters: the floor is about key material.
        if not sep or not _VERSION_RE.match(version) or len(secret_bytes) < _MIN_SECRET_BYTES:
            raise RuntimeError(
                "PRIVACY_HMAC_KEYS must be 'v1:<secret of 32+ bytes>[,v2:<secret>...]'")
        if version in ring:
            raise RuntimeError(f"PRIVACY_HMAC_KEYS lists {version} twice")
        ring[version] = _derive(secret_bytes, _INFO_PREFIX + version.encode("ascii"))
    if current not in ring:
        raise RuntimeError(f"PRIVACY_HMAC_CURRENT_VERSION {current} has no key in PRIVACY_HMAC_KEYS")
    return current, ring


def _config() -> tuple[str, dict[str, bytes]]:
    global _snapshot
    snap = _snapshot
    if snap is None:
        with _lock:
            if _snapshot is None:
                _snapshot = _load_keyring()
            snap = _snapshot
    return snap


def reset_keyring_cache() -> None:
    """Forget the parsed snapshot so the next call re-reads settings (tests)."""
    global _snapshot
    with _lock:
        _snapshot = None


def current_version() -> str:
    return _config()[0]


def live_versions() -> tuple[str, ...]:
    return tuple(sorted(_config()[1]))


def privacy_hmac(kind: str, value: str, version: str | None = None) -> str:
    """Hex HMAC-SHA256 of an ALREADY-CANONICAL value under one key version.

    Normalisation belongs to the caller (src/privacy/subjects.py), which versions it
    separately; this function refuses anything that is not already canonical.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown privacy key kind {kind!r}")
    if not isinstance(value, str) or not value.strip():
        # An empty value would make one digest that matches every empty field.
        raise ValueError("refusing to key an empty value")
    if _CANONICAL[kind](value) != value:
        raise ValueError(
            f"{kind} value is not in canonical form; normalise it with src.privacy.subjects")
    current, ring = _config()
    v = version or current
    if v not in ring:
        raise KeyError(f"privacy key version {v} is not live")
    msg = b"privacy|" + kind.encode("ascii") + b"|" + value.encode("utf-8")
    return hmac.new(ring[v], msg, hashlib.sha256).hexdigest()


def privacy_hmacs(kind: str, value: str) -> dict[str, str]:
    """The digest under EVERY live version, for rules made or matched mid-rotation."""
    _current, ring = _config()  # one snapshot for every version listed
    return {v: privacy_hmac(kind, value, v) for v in sorted(ring)}
