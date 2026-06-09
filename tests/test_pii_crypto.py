"""H3 Phase 1: PII field-encryption primitives (pure-function, no DB).

Exercises the version-prefixed Fernet scheme, tolerant/strict decryption, the
decrypt-validated ciphertext detection (the fe1: collision case), the email
blind index, and the EncryptedString/EncryptedJSON SQLAlchemy types directly —
no Postgres/Redis needed.
"""

import json

import pytest
from cryptography.fernet import InvalidToken

from src.config import settings
from src.db.encrypted_types import EncryptedJSON, EncryptedString
from src.utils.crypto import (
    _ENC_PREFIX,
    blind_index,
    decrypt_field,
    encrypt_field,
    is_encrypted,
    normalize_email,
)


@pytest.fixture
def tolerant(monkeypatch):
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", False)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", True)


# ─── encrypt / decrypt round-trip ─────────────────────────────────────────────

def test_encrypt_field_is_prefixed():
    token = encrypt_field("206-555-0100")
    assert token.startswith(_ENC_PREFIX)


def test_round_trip(tolerant):
    for value in ["206-555-0100", "owner@example.com", "Iñtërn<unicode>", "x" * 4000]:
        assert decrypt_field(encrypt_field(value)) == value


def test_encrypt_is_nondeterministic():
    # Fernet embeds a random IV — same plaintext, different ciphertext.
    assert encrypt_field("a@b.com") != encrypt_field("a@b.com")


def test_encrypt_none_raises():
    with pytest.raises(ValueError):
        encrypt_field(None)


# ─── tolerant vs strict on legacy plaintext ───────────────────────────────────

def test_legacy_plaintext_passthrough_tolerant(tolerant):
    assert decrypt_field("206-555-0100") == "206-555-0100"


def test_legacy_plaintext_rejected_strict(strict):
    with pytest.raises(InvalidToken):
        decrypt_field("206-555-0100")


def test_valid_ciphertext_decrypts_in_strict_mode(strict):
    # Real ciphertext must still decrypt once strict mode is on.
    assert decrypt_field(encrypt_field("owner@example.com")) == "owner@example.com"


# ─── the fe1: collision case (Codex P1 #1) ────────────────────────────────────

def test_plaintext_starting_with_prefix_round_trips(tolerant):
    # A real value that literally starts with "fe1:" must survive encryption.
    value = "fe1:not-actually-ciphertext"
    assert decrypt_field(encrypt_field(value)) == value


def test_prefixed_but_invalid_is_legacy_in_tolerant(tolerant):
    # Legacy plaintext that happens to start with the sentinel -> returned as-is.
    assert decrypt_field("fe1:hello world") == "fe1:hello world"


def test_prefixed_but_invalid_raises_in_strict(strict):
    with pytest.raises(InvalidToken):
        decrypt_field("fe1:hello world")


# ─── is_encrypted (backfill idempotency) ──────────────────────────────────────

def test_is_encrypted():
    assert is_encrypted(encrypt_field("x@y.com")) is True
    assert is_encrypted("x@y.com") is False
    assert is_encrypted("fe1:garbage-not-a-token") is False
    assert is_encrypted(None) is False
    assert is_encrypted("") is False


# ─── blind index ──────────────────────────────────────────────────────────────

def test_blind_index_deterministic_and_normalized():
    a = blind_index("Owner@Example.com")
    b = blind_index("  owner@example.com  ")
    c = blind_index("OWNER@EXAMPLE.COM")
    assert a == b == c


def test_blind_index_distinguishes_values():
    assert blind_index("a@example.com") != blind_index("b@example.com")


def test_blind_index_is_hex_sha256():
    idx = blind_index("a@example.com")
    assert len(idx) == 64
    int(idx, 16)  # parses as hex


def test_blind_index_none_raises():
    with pytest.raises(ValueError):
        blind_index(None)


def test_normalize_email_casefold():
    assert normalize_email("  ÀÉ@Example.COM ") == "àé@example.com"


# ─── EncryptedString type ─────────────────────────────────────────────────────

def test_encrypted_string_bind_and_result(tolerant):
    t = EncryptedString()
    bound = t.process_bind_param("206-555-0100", None)
    assert bound.startswith(_ENC_PREFIX)
    assert t.process_result_value(bound, None) == "206-555-0100"


def test_encrypted_string_none_and_blank_to_null():
    t = EncryptedString()
    assert t.process_bind_param(None, None) is None
    assert t.process_bind_param("", None) is None
    assert t.process_bind_param("   ", None) is None
    assert t.process_result_value(None, None) is None


def test_encrypted_string_reads_legacy_plaintext(tolerant):
    # A column mid-backfill still holding plaintext reads correctly.
    t = EncryptedString()
    assert t.process_result_value("legacy@plain.com", None) == "legacy@plain.com"


# ─── EncryptedJSON type ───────────────────────────────────────────────────────

def test_encrypted_json_round_trip(tolerant):
    t = EncryptedJSON()
    value = [{"number": "206-555-0100", "type": "Mobile"}, {"number": "206-555-0101", "type": None}]
    bound = t.process_bind_param(value, None)
    assert bound.startswith(_ENC_PREFIX)
    assert t.process_result_value(bound, None) == value


def test_encrypted_json_empty_list_is_not_null(tolerant):
    t = EncryptedJSON()
    bound = t.process_bind_param([], None)
    assert bound is not None and bound.startswith(_ENC_PREFIX)
    assert t.process_result_value(bound, None) == []


def test_encrypted_json_none_passthrough():
    t = EncryptedJSON()
    assert t.process_bind_param(None, None) is None
    assert t.process_result_value(None, None) is None


def test_encrypted_json_reads_legacy_plaintext(tolerant):
    # JSON->TEXT migration leaves legacy rows as plaintext JSON text.
    t = EncryptedJSON()
    legacy = json.dumps(["a@b.com"])
    assert t.process_result_value(legacy, None) == ["a@b.com"]
