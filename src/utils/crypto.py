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
import threading

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.config import settings

_HKDF_INFO = b"bridgeleads:field-encryption:v1"

_fernet: MultiFernet | None = None
_lock = threading.Lock()


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
    """Encrypt a string for storage in a DB column. Returns a urlsafe token."""
    if plaintext is None:
        raise ValueError("encrypt_field() requires a non-None plaintext")
    return _instance().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_field(token: str) -> str:
    """Decrypt a token produced by encrypt_field(). Raises cryptography
    InvalidToken if the ciphertext is corrupt or not decryptable by any key."""
    return _instance().decrypt(token.encode("ascii")).decode("utf-8")
