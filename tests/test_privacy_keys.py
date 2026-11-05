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
    assert keys.privacy_hmac("person", "main") != keys.privacy_hmac("property", "main")


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
    "v1:short",                 # secret under 32 chars
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


def test_dev_fallback_works_but_production_and_strict_refuse_it(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", "")
    keys.reset_keyring_cache()
    assert len(keys.privacy_hmac("person", "smith|john")) == 64
    for env, strict in (("production", False), ("test", True)):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
        monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", strict)
        keys.reset_keyring_cache()
        with pytest.raises(RuntimeError):
            keys.privacy_hmac("person", "smith|john")
