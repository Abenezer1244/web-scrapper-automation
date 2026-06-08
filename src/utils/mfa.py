"""TOTP + backup-code helpers for MFA (H2).

TOTP via pyotp (RFC 6238). Backup codes are random high-entropy strings stored
as keyed HMAC-SHA256 digests (pepper = SECRET_KEY) — never plaintext — so a DB
read does not yield usable codes, and verification is cheap + constant-time.

The TOTP secret itself is encrypted at rest by src/utils/crypto.py before it is
stored on User.mfa_secret_encrypted; this module only deals with the plaintext
secret in memory during setup/verify.
"""

import hashlib
import hmac
import secrets

import pyotp

from src.config import settings

_TOTP_ISSUER = "BridgeLeads"
_BACKUP_CODE_COUNT = 10
# TOTP verify window: accept the adjacent 30s steps (±30s) to tolerate clock
# skew between the user's authenticator and the server.
_TOTP_VALID_WINDOW = 1


def generate_totp_secret() -> str:
    """A fresh base32 TOTP secret."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account_email: str) -> str:
    """otpauth:// URI for QR display / authenticator import."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_email, issuer_name=_TOTP_ISSUER
    )


def verify_totp(secret: str, code: str) -> bool:
    """True if `code` is valid for `secret` now (±1 step). Tolerant of spaces."""
    if not secret or not code:
        return False
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(cleaned, valid_window=_TOTP_VALID_WINDOW)
    except Exception:
        return False


def _pepper() -> bytes:
    return (settings.SECRET_KEY or "bridgeleads-mfa-pepper").encode("utf-8")


def _normalize_backup_code(code: str) -> str:
    return (code or "").strip().replace("-", "").replace(" ", "").lower()


def hash_backup_code(code: str) -> str:
    """Keyed HMAC-SHA256 hex digest of a normalized backup code (for storage)."""
    return hmac.new(_pepper(), _normalize_backup_code(code).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_backup_code_hash(code: str, stored_hash: str) -> bool:
    """Constant-time compare of a candidate code against a stored hash."""
    if not code or not stored_hash:
        return False
    return hmac.compare_digest(hash_backup_code(code), stored_hash)


def generate_backup_codes(n: int = _BACKUP_CODE_COUNT) -> tuple[list[str], list[str]]:
    """Return (plaintext_codes, hashes). Plaintext is shown to the user ONCE;
    only the hashes are persisted. Format: 'xxxxx-xxxxx' (10 hex chars)."""
    plaintext: list[str] = []
    hashes: list[str] = []
    for _ in range(n):
        raw = secrets.token_hex(5)  # 10 hex chars, ~40 bits
        code = f"{raw[:5]}-{raw[5:]}"
        plaintext.append(code)
        hashes.append(hash_backup_code(code))
    return plaintext, hashes
