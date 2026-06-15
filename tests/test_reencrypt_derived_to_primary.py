"""Test the derived->primary re-encryption transform used to recover the 61
`users.email` rows stranded on the HKDF-derived key (incident 2026-06-15).

Real crypto, no mocks: encrypt a value under a 'derived' Fernet key, then assert
the transform produces a token that decrypts under the 'primary' (current) key to
the SAME plaintext, and that idempotent re-runs / non-derived inputs are skipped.
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from scripts.diag_undecryptable_pii import reencrypt_derived_to_primary

from src.config import settings
from src.utils import crypto

_PRIMARY = Fernet.generate_key().decode("ascii")
_DERIVED = Fernet.generate_key().decode("ascii")


@pytest.fixture
def primary_env():
    """Point encrypt_field at a known PRIMARY key (reset the lazy singleton)."""
    saved_key, saved_fernet = settings.FIELD_ENCRYPTION_KEY, crypto._fernet
    settings.FIELD_ENCRYPTION_KEY = _PRIMARY
    crypto._fernet = None  # force _build_fernet() to rebuild on next encrypt_field
    yield
    settings.FIELD_ENCRYPTION_KEY, crypto._fernet = saved_key, saved_fernet


def _enc(fernet: Fernet, plaintext: str) -> str:
    return crypto._ENC_PREFIX + fernet.encrypt(plaintext.encode()).decode("ascii")


def test_derived_token_reencrypts_onto_primary(primary_env):
    derived = Fernet(_DERIVED)
    current = MultiFernet([Fernet(_PRIMARY)])
    token = _enc(derived, "owner@example.com")

    new_val = reencrypt_derived_to_primary(token, derived=derived, current=current)

    assert new_val is not None and new_val.startswith(crypto._ENC_PREFIX)
    # Decrypts under the primary set back to the original plaintext.
    raw = new_val[len(crypto._ENC_PREFIX):].encode("ascii")
    assert current.decrypt(raw).decode() == "owner@example.com"
    # And it is NO LONGER decryptable under the derived key alone.
    with pytest.raises(InvalidToken):
        derived.decrypt(raw)


def test_already_current_token_is_skipped(primary_env):
    derived = Fernet(_DERIVED)
    current = MultiFernet([Fernet(_PRIMARY)])
    token = _enc(Fernet(_PRIMARY), "already@primary.com")
    # Already on the current key -> None (idempotent skip; never re-touched).
    assert reencrypt_derived_to_primary(token, derived=derived, current=current) is None


def test_unprefixed_value_is_skipped(primary_env):
    derived = Fernet(_DERIVED)
    current = MultiFernet([Fernet(_PRIMARY)])
    assert reencrypt_derived_to_primary("plain@text.com", derived=derived, current=current) is None
