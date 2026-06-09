"""Field-level encryption for sensitive DB columns.

Introduced for the MFA TOTP secret (H2); intended to be the shared primitive
for H3 (PII column encryption) too. Uses Fernet (AES-128-CBC + HMAC-SHA256
authenticated tokens) via cryptography.

Key resolution (in order):
  1. ``settings.FIELD_ENCRYPTION_KEY`` — one or more urlsafe-base64 Fernet keys,
     comma-separated. The FIRST is used to encrypt; ALL are tried on decrypt
     (MultiFernet), which gives a zero-downtime key-rotation path: prepend a new
     key, re-encrypt lazily, then drop the old one.
  2. Fallback — derive a key from ``settings.SECRET_KEY`` via HKDF-SHA256 with a
     fixed context label. This lets the feature work before a dedicated key is
     provisioned. Caveat: rotating SECRET_KEY then makes existing ciphertext
     undecryptable (MFA users must re-enroll), so provision a real
     FIELD_ENCRYPTION_KEY in production to decouple field encryption from JWT
     signing.

The Fernet instance is built lazily so importing this module never fails at
process start (SECRET_KEY is validated elsewhere).
"""

import base64
import hashlib
import hmac
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.config import settings

_HKDF_INFO = b"bridgeleads:field-encryption:v1"
_HKDF_INFO_BLIND_INDEX = b"bridgeleads:blind-index:v1"

# Version sentinel prepended to every ciphertext token (H3). Lets reads
# distinguish our ciphertext from legacy plaintext during the backfill window.
# Detection is decrypt-VALIDATED (see decrypt_field), never prefix-only — so a
# real plaintext value that happens to start with this sentinel is still handled
# correctly instead of being silently lost.
_ENC_PREFIX = "fe1:"

_fernet: MultiFernet | None = None
_lock = threading.Lock()

_blind_index_key: bytes | None = None
_bi_lock = threading.Lock()


def _derive_key_from_secret() -> bytes:
    """Deterministic 32-byte Fernet key derived from SECRET_KEY (fallback)."""
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(settings.SECRET_KEY.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _build_fernet() -> MultiFernet:
    configured = (settings.FIELD_ENCRYPTION_KEY or "").strip()
    if configured:
        keys = [Fernet(k.strip()) for k in configured.split(",") if k.strip()]
        if not keys:
            raise ValueError("FIELD_ENCRYPTION_KEY is set but contains no valid key")
        return MultiFernet(keys)
    return MultiFernet([Fernet(_derive_key_from_secret())])


def _instance() -> MultiFernet:
    global _fernet
    if _fernet is None:
        with _lock:
            if _fernet is None:
                _fernet = _build_fernet()
    return _fernet


def encrypt_field(plaintext: str) -> str:
    """Encrypt a string for storage in a DB column.

    Returns a versioned token: the ``fe1:`` sentinel followed by a urlsafe
    Fernet token. Callers must pass a non-None plaintext (the SQLAlchemy
    ``EncryptedString`` type handles None/blank upstream).
    """
    if plaintext is None:
        raise ValueError("encrypt_field() requires a non-None plaintext")
    token = _instance().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def _try_decrypt(token: str) -> str | None:
    """Decrypt a ``fe1:``-prefixed token; return None if it is not in fact our
    ciphertext (prefix present but body not decryptable by any active key)."""
    body = token[len(_ENC_PREFIX):]
    try:
        return _instance().decrypt(body.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def decrypt_field(value: str) -> str:
    """Return the plaintext for a stored field value.

    Detection is decrypt-validated, not prefix-only:

    * ``fe1:``-prefixed AND decryptable -> the decrypted plaintext.
    * ``fe1:``-prefixed but NOT decryptable -> treated as legacy plaintext that
      happens to start with the sentinel. Returned as-is in tolerant mode; in
      strict mode this is corruption and raises ``InvalidToken``.
    * no prefix -> legacy plaintext. Returned as-is in tolerant mode; raises in
      strict mode.

    Tolerant mode (``settings.PII_ENCRYPTION_STRICT is False``) is required
    during the backfill window when a column holds a mix of ciphertext and
    plaintext. Strict mode is enabled only after backfill is verified complete.
    """
    if value.startswith(_ENC_PREFIX):
        plaintext = _try_decrypt(value)
        if plaintext is not None:
            return plaintext
        # Prefix present but not our ciphertext -> legacy plaintext or corruption.
        if settings.PII_ENCRYPTION_STRICT:
            raise InvalidToken(
                "fe1:-prefixed value is not decryptable under strict mode"
            )
        return value
    # No prefix -> legacy plaintext.
    if settings.PII_ENCRYPTION_STRICT:
        raise InvalidToken("unencrypted value rejected under strict mode")
    return value


def is_encrypted(value: str | None) -> bool:
    """True iff ``value`` is one of our fe1: ciphertext tokens (decrypt-validated).

    Used by the backfill script for idempotency: a value that is already our
    ciphertext is skipped; anything else (legacy plaintext, including a value
    that coincidentally starts with ``fe1:``) is (re-)encrypted.
    """
    if value is None or not value.startswith(_ENC_PREFIX):
        return False
    return _try_decrypt(value) is not None


def _derive_blind_index_key() -> bytes:
    """Resolve the stable HMAC key for the email blind index.

    Order: ``settings.BLIND_INDEX_KEY`` (dedicated, stable, env) -> HKDF from
    ``SECRET_KEY`` (dev fallback). Deliberately NOT derived from
    ``FIELD_ENCRYPTION_KEY`` so Fernet key rotation cannot change it.
    """
    configured = (settings.BLIND_INDEX_KEY or "").strip()
    if configured:
        return configured.encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO_BLIND_INDEX,
    ).derive(settings.SECRET_KEY.encode("utf-8"))


def _blind_index_secret() -> bytes:
    global _blind_index_key
    if _blind_index_key is None:
        with _bi_lock:
            if _blind_index_key is None:
                _blind_index_key = _derive_blind_index_key()
    return _blind_index_key


def normalize_email(value: str) -> str:
    """Canonical form for email equality matching: trimmed + Unicode case-fold."""
    return value.strip().casefold()


def blind_index(value: str) -> str:
    """Deterministic, keyed lookup token for a searchable PII value (e.g. email).

    ``HMAC-SHA256(K_BI, normalize(value))`` as hex. Equal normalized inputs map
    to equal tokens (enables equality lookup + a UNIQUE index) while a DB-only
    attacker without ``K_BI`` cannot run an offline dictionary attack.
    """
    if value is None:
        raise ValueError("blind_index() requires a non-None value")
    normalized = normalize_email(value).encode("utf-8")
    return hmac.new(_blind_index_secret(), normalized, hashlib.sha256).hexdigest()
