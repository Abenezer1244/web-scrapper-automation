"""H2-P5 Step A: amr / auth_time token claims (pure-function, no DB).

These exercise the JWT minting/decoding + amr sanitization directly, so they run
without Postgres/Redis (unlike the integration tests in test_auth.py that need
the `client`/`db` fixtures). They prove the session-strength invariants the admin
step-up dependency in Step B relies on.
"""

import time

import jwt
import pytest

from src.api.auth import (
    _ACCESS_AUDIENCE,
    _REFRESH_AUDIENCE,
    _coerce_auth_time,
    _sanitize_amr,
    create_refresh_token,
    create_secure_token,
    decode_refresh_token,
    decode_secure_token,
)
from src.config import settings

# ─── _sanitize_amr ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ["pwd"]),                       # legacy token: no amr claim
        ([], ["pwd"]),                         # empty -> baseline
        ("pwd", ["pwd"]),                      # non-list -> baseline
        (["pwd"], ["pwd"]),
        (["pwd", "mfa"], ["pwd", "mfa"]),
        (["mfa", "pwd"], ["mfa", "pwd"]),      # order preserved
        (["pwd", "pwd", "mfa"], ["pwd", "mfa"]),  # de-duped
        (["pwd", "admin", "mfa"], ["pwd", "mfa"]),  # unknown token dropped
        (["totally-bogus"], ["pwd"]),          # all-unknown -> baseline
        (["pwd", 123, None, "break_glass"], ["pwd", "break_glass"]),  # junk dropped
    ],
)
def test_sanitize_amr(raw, expected):
    assert _sanitize_amr(raw) == expected


# ─── access token carries amr + auth_time ─────────────────────────────────────

def test_access_token_default_amr_is_pwd():
    tok = create_secure_token("u1")
    payload = decode_secure_token(tok)
    assert payload["amr"] == ["pwd"]
    assert payload["purpose"] == "access"
    assert isinstance(payload["auth_time"], int)


def test_access_token_mfa_amr():
    tok = create_secure_token("u1", amr=["pwd", "mfa"])
    assert decode_secure_token(tok)["amr"] == ["pwd", "mfa"]


def test_access_token_explicit_auth_time_preserved():
    pinned = int(time.time()) - 3600
    tok = create_secure_token("u1", amr=["pwd", "mfa"], auth_time=pinned)
    assert decode_secure_token(tok)["auth_time"] == pinned


def test_access_token_sanitizes_unknown_amr():
    tok = create_secure_token("u1", amr=["pwd", "root", "mfa"])
    assert decode_secure_token(tok)["amr"] == ["pwd", "mfa"]


# ─── refresh token carries the same claims under its own audience ─────────────

def test_refresh_token_carries_amr_and_auth_time():
    pinned = int(time.time()) - 120
    tok = create_refresh_token("u1", amr=["pwd", "mfa"], auth_time=pinned)
    payload = decode_refresh_token(tok)
    assert payload["amr"] == ["pwd", "mfa"]
    assert payload["auth_time"] == pinned
    assert payload["purpose"] == "refresh"


def test_refresh_token_audience_isolated_from_access():
    """A refresh token must never decode on the access path (and vice-versa)."""
    refresh = create_refresh_token("u1", amr=["pwd", "mfa"])
    with pytest.raises(jwt.InvalidTokenError):
        decode_secure_token(refresh)
    access = create_secure_token("u1", amr=["pwd", "mfa"])
    with pytest.raises(jwt.InvalidTokenError):
        decode_refresh_token(access)


# ─── refresh propagation rules (the /auth/refresh logic, modelled directly) ───
# /auth/refresh sanitizes the incoming amr and copies amr+auth_time UNCHANGED
# into the rotated pair: never adds "mfa", never drops it.

def _simulate_refresh(old_refresh: str) -> tuple[str, str]:
    # Mirrors the production /auth/refresh logic exactly, including the
    # fail-closed freshness rule: a missing/garbage auth_time becomes 0 (stale),
    # never "now" — so a refresh can't silently mint a FRESH mfa session.
    payload = decode_refresh_token(old_refresh)
    amr = _sanitize_amr(payload.get("amr"))
    auth_time = _coerce_auth_time(payload.get("auth_time"))
    if auth_time is None:
        auth_time = 0
    return (
        create_secure_token(payload["sub"], amr=amr, auth_time=auth_time),
        create_refresh_token(payload["sub"], amr=amr, auth_time=auth_time),
    )


def test_refresh_preserves_mfa_amr_and_auth_time():
    pinned = int(time.time()) - 300
    old = create_refresh_token("u1", amr=["pwd", "mfa"], auth_time=pinned)
    new_access, new_refresh = _simulate_refresh(old)
    ap = decode_secure_token(new_access)
    rp = decode_refresh_token(new_refresh)
    assert ap["amr"] == ["pwd", "mfa"]
    assert rp["amr"] == ["pwd", "mfa"]
    # auth_time copied unchanged — refresh does NOT reset step-up freshness.
    assert ap["auth_time"] == pinned
    assert rp["auth_time"] == pinned


def test_refresh_never_adds_mfa():
    old = create_refresh_token("u1", amr=["pwd"])
    new_access, _ = _simulate_refresh(old)
    assert decode_secure_token(new_access)["amr"] == ["pwd"]


def test_refresh_of_legacy_token_without_amr_defaults_to_pwd():
    """A pre-P5 refresh token (no amr/auth_time claim) refreshes to a pwd-only
    session — it can never silently become MFA-capable."""
    now = int(time.time())
    legacy = jwt.encode(
        {
            "sub": "u1",
            "jti": "legacy-jti",
            "iss": "bridgeleads",
            "aud": _REFRESH_AUDIENCE,
            "purpose": "refresh",
            "iat": now,
            "exp": now + 3600,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    new_access, _ = _simulate_refresh(legacy)
    assert decode_secure_token(new_access)["amr"] == ["pwd"]


def test_access_audience_constant_unchanged():
    """Guard against an accidental audience rename breaking token isolation."""
    assert _ACCESS_AUDIENCE == "bridgeleads-api"
    assert _REFRESH_AUDIENCE == "bridgeleads-refresh"


# ─── break-glass amr survives refresh (recovery session can't self-upgrade) ───

def test_break_glass_amr_preserved_through_refresh():
    """A break-glass refresh token stays break_glass on rotation and never gains
    'mfa' — the recovery session can't self-upgrade to step-up-capable."""
    old = create_refresh_token("u1", amr=["pwd", "break_glass"])
    new_access, _ = _simulate_refresh(old)
    amr = decode_secure_token(new_access)["amr"]
    assert amr == ["pwd", "break_glass"]
    assert "mfa" not in amr


# ─── Codex P1: refresh must NOT mint a FRESH session for a missing auth_time ──

def test_refresh_with_mfa_amr_but_missing_auth_time_is_stale_not_fresh():
    """An mfa refresh token with NO auth_time claim must NOT rotate into a fresh
    (now-stamped) MFA session — it would silently pass step-up freshness. The
    rotated session keeps amr=["pwd","mfa"] but auth_time is forced to 0 (stale),
    so step-up requires a real re-auth at /auth/login/mfa."""
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "u1",
            "jti": "no-authtime",
            "iss": "bridgeleads",
            "aud": _REFRESH_AUDIENCE,
            "purpose": "refresh",
            "amr": ["pwd", "mfa"],  # strong session...
            # ...but NO auth_time claim.
            "iat": now,
            "exp": now + 3600,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    new_access, _ = _simulate_refresh(forged)
    ap = decode_secure_token(new_access)
    assert ap["amr"] == ["pwd", "mfa"]  # strength preserved (no downgrade)
    # The key invariant: NOT freshly stamped. 0 is the stale epoch sentinel.
    assert ap["auth_time"] == 0
    assert ap["auth_time"] < now - 60  # unambiguously not "now"


def test_refresh_with_garbage_auth_time_is_stale_not_fresh():
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "u1",
            "jti": "garbage-authtime",
            "iss": "bridgeleads",
            "aud": _REFRESH_AUDIENCE,
            "purpose": "refresh",
            "amr": ["pwd", "mfa"],
            "auth_time": "not-an-int",
            "iat": now,
            "exp": now + 3600,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    new_access, _ = _simulate_refresh(forged)
    assert decode_secure_token(new_access)["auth_time"] == 0


# ─── Codex P3: strict auth_time typing (bool is a subclass of int) ────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        (5, 5),
        (0, 0),
        (-1, -1),
        (True, None),      # bool rejected even though bool ⊂ int
        (False, None),
        (5.0, None),       # float rejected
        ("5", None),       # string rejected
        (None, None),
    ],
)
def test_coerce_auth_time_strict(raw, expected):
    assert _coerce_auth_time(raw) == expected


def test_token_with_bool_auth_time_does_not_become_epoch_one():
    """auth_time=True must NOT silently coerce to 1 in a minted token — the
    minter rejects the bool and stamps a real current timestamp instead."""
    now = int(time.time())
    tok = create_secure_token("u1", amr=["pwd"], auth_time=True)  # type: ignore[arg-type]
    at = decode_secure_token(tok)["auth_time"]
    assert at != 1
    assert at >= now - 5
