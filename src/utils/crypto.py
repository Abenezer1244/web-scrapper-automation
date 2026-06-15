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
    # No FIELD_ENCRYPTION_KEY configured. Silently deriving a key from SECRET_KEY is
    # what stranded 61 users.email as undecryptable in 2026-06: a process running a
    # window without the key encrypted PII under the derived key, which then became
    # unreadable once the real key was (re)provisioned. In production OR strict mode
    # this is never legitimate — REFUSE rather than silently encrypt under the
    # ephemeral fallback. (Belt AND suspenders: prod runs PII_ENCRYPTION_STRICT=true
    # today, but a future window with STRICT=false + a missing key would re-open the
    # exact failure mode, so we also fail on ENVIRONMENT=production.) The HKDF
    # fallback stays available only for non-prod, non-strict local dev / tests.
    if settings.PII_ENCRYPTION_STRICT or settings.ENVIRONMENT.strip().lower() == "production":
        raise RuntimeError(
            "FIELD_ENCRYPTION_KEY is not set in production / strict mode. Refusing to "
            "fall back to the SECRET_KEY-derived key — it strands PII as undecryptable "
            "once a real key is provisioned. Set FIELD_ENCRYPTION_KEY on every api+worker."
        )
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


def _fernet_decrypt(token: str) -> str | None:
    """Try to Fernet-decrypt a raw token (no fe1: prefix); None if it is not a
    valid token under any active key.

    ``_instance()`` is called OUTSIDE the catch so a key/config error
    (malformed ``FIELD_ENCRYPTION_KEY``) propagates and fails fast, rather than
    being mistaken for "not a token" and silently passed through as plaintext.
    Only token-level decode failures are suppressed.
    """
    fernet = _instance()
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        return None  # non-ascii -> cannot be a base64url Fernet token
    try:
        return fernet.decrypt(raw).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def decrypt_field(value: str) -> str:
    """Return the plaintext for a stored field value.

    Detection is decrypt-validated, not prefix-only:

    * ``fe1:``-prefixed AND decryptable -> the decrypted plaintext.
    * ``fe1:``-prefixed but NOT decryptable -> legacy plaintext that happens to
      start with the sentinel. Returned as-is in tolerant mode; raises in strict.
    * no prefix AND a valid bare Fernet token -> the decrypted plaintext. This
      preserves values written by the pre-H3 ``encrypt_field`` (which emitted
      bare, unprefixed Fernet tokens), most importantly the H2 MFA secret
      (``users.mfa_secret_encrypted``). Without this, existing MFA enrollments
      would decrypt to raw ciphertext and TOTP login would break.
    * no prefix AND not a Fernet token -> legacy plaintext (PII mid-backfill).
      Returned as-is in tolerant mode; raises in strict mode.

    Tolerant mode (``settings.PII_ENCRYPTION_STRICT is False``) is required
    during the backfill window when a column holds a mix of ciphertext and
    plaintext. Strict mode is enabled only after backfill is verified complete.
    Bare legacy ciphertext still decrypts in strict mode (it is encrypted at
    rest); strict only rejects genuine plaintext.
    """
    if value.startswith(_ENC_PREFIX):
        plaintext = _fernet_decrypt(value[len(_ENC_PREFIX):])
        if plaintext is not None:
            return plaintext
        if settings.PII_ENCRYPTION_STRICT:
            raise InvalidToken(
                "fe1:-prefixed value is not decryptable under strict mode"
            )
        return value
    # No prefix: a legacy bare Fernet token (pre-H3, e.g. MFA secret) or plaintext.
    legacy = _fernet_decrypt(value)
    if legacy is not None:
        return legacy
    if settings.PII_ENCRYPTION_STRICT:
        raise InvalidToken("unencrypted value rejected under strict mode")
    return value


def is_encrypted(value: str | None) -> bool:
    """True iff ``value`` is ciphertext (decrypt-validated): an fe1: token, or a
    legacy bare Fernet token.

    Backfill idempotency: an already-encrypted value is skipped; legacy plaintext
    (including a value that coincidentally starts with ``fe1:``) is encrypted.
    Returning True for bare Fernet tokens prevents a pre-H3 ciphertext from being
    double-encrypted by the backfill.
    """
    if value is None or value == "":
        return False
    if value.startswith(_ENC_PREFIX):
        return _fernet_decrypt(value[len(_ENC_PREFIX):]) is not None
    return _fernet_decrypt(value) is not None


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
