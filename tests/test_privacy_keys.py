"""The versioned privacy keyring (src/privacy/keys.py; Phase 6 design, section 5).

Pure: settings are set per test with pytest's monkeypatch, and the parsed keyring
cache is reset around each test.
"""

import pytest

from src.config import settings
from src.privacy import keys

_S1 = "a" * 40
_S2 = "b" * 40


@pytest.fixture(autouse=True)
def _fresh_keyring(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", False)
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v1:{_S1}")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "v1")
    keys.reset_keyring_cache()
    yield
    keys.reset_keyring_cache()


def test_digest_is_deterministic_hex_and_keyed():
    d = keys.privacy_hmac("person", "smith|john")
    assert d == keys.privacy_hmac("person", "smith|john")
    assert len(d) == 64 and int(d, 16) >= 0
    # Not a plain hash of the value: anyone without the key cannot recompute it.
    import hashlib
    assert d != hashlib.sha256(b"smith|john").hexdigest()


def test_kinds_are_domain_separated():
    assert keys.privacy_hmac("entity", "main") != keys.privacy_hmac("property", "main")


@pytest.mark.parametrize(("kind", "value"), [
    ("person", "Smith|John"),           # not casefolded
    ("person", "smith john"),           # no separator
    ("person", "smith|john paul"),      # given is the FIRST given name only
    ("entity", "ACME LLC"),
    ("entity", "acme  llc"),
    ("phone", "(206) 555-1234"),
    ("phone", "12065551234"),
    ("email", "Jane@Example.com"),
    ("email", "not-an-email"),
    ("property", " 123 main st"),
])
def test_non_canonical_values_are_refused(kind, value):
    # Two spellings of one value would make two digests that never meet.
    with pytest.raises(ValueError):
        keys.privacy_hmac(kind, value)


def test_canonical_values_from_the_normalisers_are_accepted():
    from src.privacy.subjects import email_key_value, person_key_value, phone_key_value
    for kind, value in (("person", person_key_value("Núñez", "José María")),
                        ("phone", phone_key_value("(206) 555-1234")),
                        ("email", email_key_value(" Jane@Example.com "))):
        assert len(keys.privacy_hmac(kind, value)) == 64


def test_unknown_kind_and_empty_value_are_refused():
    with pytest.raises(ValueError):
        keys.privacy_hmac("nickname", "x")
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            keys.privacy_hmac("person", empty)


def test_rotation_keeps_old_digests_matchable_and_versions_never_collide(monkeypatch):
    before = keys.privacy_hmac("person", "smith|john")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v1:{_S1},v2:{_S2}")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "v2")
    keys.reset_keyring_cache()
    both = keys.privacy_hmacs("person", "smith|john")
    assert set(both) == {"v1", "v2"}
    assert both["v1"] == before                    # old rules still match
    assert keys.privacy_hmac("person", "smith|john") == both["v2"]  # new rules use v2
    assert both["v1"] != both["v2"]
    # The version is in the key derivation: the same secret under two versions
    # still yields different keys.
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v1:{_S1},v2:{_S1}")
    keys.reset_keyring_cache()
    same_secret = keys.privacy_hmacs("person", "smith|john")
    assert same_secret["v1"] != same_secret["v2"]


def test_an_unlisted_version_is_refused():
    with pytest.raises(KeyError):
        keys.privacy_hmac("person", "smith|john", version="v9")


@pytest.mark.parametrize("bad", [
    "v1",                       # no secret
    f"1:{_S1}",                 # bad version label
    "v1:short",                 # secret under 32 bytes
    "v1:" + "é" * 15,           # 15 chars, 30 bytes: the floor is bytes
    f"v1:{_S1},v1:{_S2}",       # duplicate version
])
def test_malformed_keyring_fails_loudly(monkeypatch, bad):
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", bad)
    keys.reset_keyring_cache()
    with pytest.raises(RuntimeError):
        keys.privacy_hmac("person", "smith|john")


def test_current_version_must_have_a_key(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "v2")
    keys.reset_keyring_cache()
    with pytest.raises(RuntimeError):
        keys.privacy_hmac("person", "smith|john")


def test_a_multibyte_secret_of_32_bytes_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", "v1:" + "é" * 16)
    keys.reset_keyring_cache()
    assert len(keys.privacy_hmac("person", "smith|john")) == 64


def test_dev_fallback_only_in_listed_dev_and_test_environments(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", "")
    for env in ("test", " Development ", "local"):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
        keys.reset_keyring_cache()
        assert len(keys.privacy_hmac("person", "smith|john")) == 64
    # Fails closed for anything else, a misspelling or an unexpected name included.
    for env in ("production", "Production ", "prod", "staging", ""):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
        keys.reset_keyring_cache()
        with pytest.raises(RuntimeError):
            keys.privacy_hmac("person", "smith|john")
    # PII strict mode means "behave like production", even in a test environment.
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", True)
    keys.reset_keyring_cache()
    with pytest.raises(RuntimeError):
        keys.privacy_hmac("person", "smith|john")


def test_settings_are_read_once_as_one_snapshot(monkeypatch):
    first = keys.privacy_hmac("person", "smith|john")
    # A settings change without a restart (or reset) changes nothing: no call can
    # mix the new current version with the old keyring.
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v2:{_S2}")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "v2")
    assert keys.current_version() == "v1"
    assert keys.live_versions() == ("v1",)
    assert keys.privacy_hmac("person", "smith|john") == first
    keys.reset_keyring_cache()
    assert keys.current_version() == "v2"
    assert keys.privacy_hmac("person", "smith|john") != first
