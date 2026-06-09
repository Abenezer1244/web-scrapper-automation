"""H2-P5 Step B: require_admin / require_admin_mfa dependencies (pure, no DB).

These call the dependency functions directly with a hand-built AuthContext, so
they run without Postgres/Redis. They prove the admin-enforcement + MFA step-up
matrix: 404 for non-admins, 403 enrollment-required for no-MFA admins, and 403
step-up-required for stale / non-MFA / API-key sessions.
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.auth import (
    _ADMIN_STEP_UP_FUTURE_SKEW_SECONDS,
    _ADMIN_STEP_UP_MAX_AGE_SECONDS,
    AuthContext,
    require_admin,
    require_admin_mfa,
)


def _ctx(
    *,
    is_admin=True,
    mfa_enabled=True,
    auth_method="jwt",
    amr=("pwd", "mfa"),
    auth_time="now",
):
    if auth_time == "now":
        auth_time = int(time.time())
    return AuthContext(
        user=SimpleNamespace(id="u1", is_admin=is_admin, mfa_enabled=mfa_enabled),
        auth_method=auth_method,
        amr=list(amr),
        auth_time=auth_time,
        jti="jti-1" if auth_method == "jwt" else None,
        payload={} if auth_method == "jwt" else None,
    )


# ─── require_admin ────────────────────────────────────────────────────────────

async def test_require_admin_non_admin_gets_404():
    with pytest.raises(HTTPException) as exc:
        await require_admin(_ctx(is_admin=False))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not found"  # endpoint hidden, not "forbidden"


async def test_require_admin_no_mfa_admin_gets_403_enrollment():
    with pytest.raises(HTTPException) as exc:
        await require_admin(_ctx(is_admin=True, mfa_enabled=False))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_enrollment_required"


async def test_require_admin_enrolled_admin_passes():
    ctx = _ctx(is_admin=True, mfa_enabled=True)
    assert await require_admin(ctx) is ctx


async def test_require_admin_no_mfa_admin_via_api_key_still_enrollment():
    # An admin on an API-key session who hasn't enrolled MFA must still be told
    # to enroll (404-vs-403 ordering: admin check passes, enrollment check fails).
    with pytest.raises(HTTPException) as exc:
        await require_admin(
            _ctx(is_admin=True, mfa_enabled=False, auth_method="api_key", amr=())
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_enrollment_required"


# ─── require_admin_mfa (step-up) ──────────────────────────────────────────────
# These pass an already admin+enrolled ctx (i.e. require_admin would have
# returned it) and exercise only the step-up layer.

async def test_step_up_fresh_mfa_jwt_passes():
    ctx = _ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time="now")
    assert await require_admin_mfa(ctx) is ctx


async def test_step_up_api_key_always_fails():
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="api_key", amr=(), auth_time=None))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_pwd_only_session_fails():
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="jwt", amr=("pwd",), auth_time="now"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_stale_mfa_session_fails():
    stale = int(time.time()) - _ADMIN_STEP_UP_MAX_AGE_SECONDS - 60
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=stale))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_missing_auth_time_fails():
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=None))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_zero_sentinel_auth_time_fails():
    # auth_time=0 is the stale sentinel a refresh substitutes for a missing one;
    # it must NOT pass step-up.
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=0))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_just_within_window_passes():
    edge = int(time.time()) - _ADMIN_STEP_UP_MAX_AGE_SECONDS + 5
    ctx = _ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=edge)
    assert await require_admin_mfa(ctx) is ctx


async def test_step_up_wildly_future_auth_time_fails():
    # A meaningfully-future auth_time (minting bug / tampered clock) must NOT read
    # as perpetually fresh (Codex P3).
    future = int(time.time()) + _ADMIN_STEP_UP_FUTURE_SKEW_SECONDS + 600
    with pytest.raises(HTTPException) as exc:
        await require_admin_mfa(_ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=future))
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_mfa_step_up_required"


async def test_step_up_small_future_skew_tolerated():
    # A few seconds of clock jitter is fine — within the skew tolerance.
    skewed = int(time.time()) + 5
    ctx = _ctx(auth_method="jwt", amr=("pwd", "mfa"), auth_time=skewed)
    assert await require_admin_mfa(ctx) is ctx
