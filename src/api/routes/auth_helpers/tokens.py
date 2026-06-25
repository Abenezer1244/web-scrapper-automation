"""Stateless auth token primitives (reset + MFA-challenge) and the login 2nd-factor
consume. Moved VERBATIM out of auth.py so the route handlers stay thin; auth.py
re-exports every name here so existing `from src.api.routes.auth import X` (and the
handler bodies) keep working unchanged.
"""

import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db import User

# ─── A3: password-reset token (stateless) ─────────────────────────────────────
# A3 finding: there is no forgot/reset-password flow, so a user whose password
# is compromised has no recovery path. We reuse the existing JWT/HS256 machinery
# (same SECRET_KEY as src/api/auth.py) rather than add a DB table, but mint reset
# tokens under a DISTINCT audience so the two token families cannot cross over:
#   - a session/access token (aud="bridgeleads-api") CANNOT reset a password
#     (verify below pins audience="bridgeleads-reset"), and
#   - a reset token CANNOT authenticate a request (get_current_user's
#     decode_secure_token pins audience="bridgeleads-api").
# We deliberately do NOT reuse decode_secure_token here because it hard-codes
# aud="bridgeleads-api"; we call jwt.* directly with the reset audience.

_RESET_TOKEN_PURPOSE = "reset"
_RESET_TOKEN_AUDIENCE = "bridgeleads-reset"
_RESET_TOKEN_ISSUER = "bridgeleads"
_RESET_TOKEN_ALGORITHM = "HS256"
_RESET_TOKEN_EXPIRE_SECONDS = 30 * 60  # short-lived: ~30 minutes


def _mint_reset_token(user_id: str) -> str:
    """Mint a short-lived, single-use-able password-reset JWT (A3).

    Fresh jti so TokenBlacklist.consume_once can burn it exactly once on
    redemption. Signed with the app SECRET_KEY/HS256, scoped to the reset
    audience so it is useless on any other endpoint.
    """
    import jwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": _RESET_TOKEN_ISSUER,
        "aud": _RESET_TOKEN_AUDIENCE,
        "purpose": _RESET_TOKEN_PURPOSE,
        "iat": now,
        "exp": now + _RESET_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_RESET_TOKEN_ALGORITHM)


def _decode_reset_token(token: str) -> dict:
    """Decode + verify a password-reset JWT (A3). Raises jwt.InvalidTokenError.

    Pins audience="bridgeleads-reset" and issuer="bridgeleads" (so a session
    token cannot be used here) and checks purpose=="reset". Caller catches
    jwt.InvalidTokenError ONLY and maps it to a generic 400 — any non-JWT
    error must surface, not be masked as "invalid link".
    """
    import jwt

    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_RESET_TOKEN_ALGORITHM],
        audience=_RESET_TOKEN_AUDIENCE,
        issuer=_RESET_TOKEN_ISSUER,
        options={"verify_exp": True},
    )
    if payload.get("purpose") != _RESET_TOKEN_PURPOSE:
        # Right audience but wrong purpose — treat as an invalid reset token.
        raise jwt.InvalidTokenError("not a reset token")
    return payload


# ─── Email-verification token (stateless) ─────────────────────────────────────
# Issued by /auth/register (EMAIL_VERIFICATION_ENABLED flow) and emailed to the
# address being registered. Redeemed at /auth/verify-email to turn a
# pending_registrations row into a real account. Like the reset token it reuses
# the app SECRET_KEY/HS256 under a DISTINCT audience so the families can't cross
# over: a verify token cannot authenticate an API request (decode_secure_token
# pins aud="bridgeleads-api") and a session/reset token cannot redeem a
# verification (_decode_verify_token pins aud="bridgeleads-verify").
#   sub = pending_registrations.id  (NOT a users.id — the account doesn't exist
#         yet). jti enables single-use burn via TokenBlacklist.consume_once.
# TTL ~24h: a verification link is less sensitive than a reset link and users
# often confirm later (different inbox, mobile), so it is longer-lived than the
# 30-min reset token but still bounded.

_VERIFY_TOKEN_PURPOSE = "verify"
_VERIFY_TOKEN_AUDIENCE = "bridgeleads-verify"
_VERIFY_TOKEN_ISSUER = "bridgeleads"
_VERIFY_TOKEN_ALGORITHM = "HS256"
_VERIFY_TOKEN_EXPIRE_SECONDS = 24 * 60 * 60  # ~24 hours


def _mint_verify_token(pending_id: str, expires_at: datetime) -> str:
    """Mint an email-verification JWT whose exp matches the pending row's deadline.

    `sub` is the pending_registrations row id (not a user id). `exp` is pinned to
    the row's own `expires_at` — NOT now()+24h — because the token is minted at
    SEND time by the dispatcher, which may be well after signup (a delayed/
    recovered send). Deriving exp from the row guarantees the link can never
    outlive the row it redeems (a JWT valid after the row is purged would 400
    confusingly) nor under-live it.

    Single use is structural, not jti-based: redeeming the link creates the
    account and deletes every pending row for the address, so any later click
    finds no row -> 400, and UNIQUE(users.email_hmac) blocks a second account
    under any race. The jti is kept only as a unique nonce / for future
    blacklist use; verify_user_email does NOT call consume_once.
    """
    import math

    import jwt

    now = int(time.time())
    payload = {
        "sub": pending_id,
        "jti": str(uuid.uuid4()),
        "iss": _VERIFY_TOKEN_ISSUER,
        "aud": _VERIFY_TOKEN_AUDIENCE,
        "purpose": _VERIFY_TOKEN_PURPOSE,
        "iat": now,
        # Round UP so the JWT exp is never EARLIER than the row's expires_at
        # (sub-second floor could make the token reject a hair before the
        # verify endpoint's `expires_at > now` row check — Codex).
        "exp": math.ceil(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_VERIFY_TOKEN_ALGORITHM)


def _decode_verify_token(token: str) -> dict:
    """Decode + verify an email-verification JWT. Raises jwt.InvalidTokenError.

    Pins audience="bridgeleads-verify" + issuer="bridgeleads" (so a session/reset
    token cannot be used here) and checks purpose=="verify". Caller catches
    jwt.InvalidTokenError ONLY and maps it to a generic error — any non-JWT error
    must surface, not be masked as "invalid link".
    """
    import jwt

    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_VERIFY_TOKEN_ALGORITHM],
        audience=_VERIFY_TOKEN_AUDIENCE,
        issuer=_VERIFY_TOKEN_ISSUER,
        options={"verify_exp": True},
    )
    if payload.get("purpose") != _VERIFY_TOKEN_PURPOSE:
        raise jwt.InvalidTokenError("not a verification token")
    return payload


# ─── H2-P3: MFA login-challenge token (stateless) ─────────────────────────────
# Issued by /auth/login after a CORRECT password when the account has MFA
# enabled. It carries NO access privilege — it only proves "the password step
# was passed for this user" and must be redeemed at /auth/login/mfa together
# with a valid 2nd factor. Like the reset token it uses the app SECRET_KEY/HS256
# under a DISTINCT audience so the token families cannot cross over:
#   - an access token (aud="bridgeleads-api") cannot redeem an MFA challenge
#     (_decode_mfa_challenge_token pins aud="bridgeleads-mfa"), and
#   - a challenge token cannot authenticate an API request (get_current_user's
#     decode_secure_token pins aud="bridgeleads-api").
# Short-lived (~5 min): long enough to type a code, short enough to bound replay
# of the challenge itself. The per-user rate limiter on /auth/login/mfa caps how
# many 2nd-factor guesses a single challenge (or rotating challenges) can drive.

_MFA_CHALLENGE_PURPOSE = "mfa_challenge"
_MFA_CHALLENGE_AUDIENCE = "bridgeleads-mfa"
_MFA_CHALLENGE_ISSUER = "bridgeleads"
_MFA_CHALLENGE_ALGORITHM = "HS256"
_MFA_CHALLENGE_EXPIRE_SECONDS = 5 * 60  # ~5 minutes


def _mint_mfa_challenge_token(user_id: str) -> str:
    """Mint a short-lived MFA login-challenge JWT (H2-P3). No access privilege."""
    import jwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": _MFA_CHALLENGE_ISSUER,
        "aud": _MFA_CHALLENGE_AUDIENCE,
        "purpose": _MFA_CHALLENGE_PURPOSE,
        "iat": now,
        "exp": now + _MFA_CHALLENGE_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_MFA_CHALLENGE_ALGORITHM)


def _decode_mfa_challenge_token(token: str) -> dict:
    """Decode + verify an MFA challenge JWT. Raises jwt.InvalidTokenError.

    Pins audience/issuer (so a session token can't be used here) and checks
    purpose=="mfa_challenge". Caller catches jwt.InvalidTokenError ONLY and maps
    it to a generic 401 — any non-JWT error must surface, not be masked.
    """
    import jwt

    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_MFA_CHALLENGE_ALGORITHM],
        audience=_MFA_CHALLENGE_AUDIENCE,
        issuer=_MFA_CHALLENGE_ISSUER,
        options={"verify_exp": True},
    )
    if payload.get("purpose") != _MFA_CHALLENGE_PURPOSE:
        raise jwt.InvalidTokenError("not an mfa challenge token")
    return payload


async def _consume_second_factor(db: AsyncSession, user: User, code: str) -> bool:
    """Verify a login 2nd factor for `user`. Returns True iff valid.

    Order: TOTP first, else an UNUSED backup code. Both are single-use and
    consumed ATOMICALLY so two concurrent logins can never both succeed on the
    same factor (Codex H2-P3/P4). Caller commits on a True return; on False
    nothing is changed (0 rows matched) and the session rolls back.

    TOTP replay (H2-P4): a code is valid for the pyotp ±1 (~90s) window, so we
    record the matched 30s timestep counter and advance `mfa_last_totp_counter`
    with a conditional UPDATE (`... < :counter`). A code thus works exactly once:
    the second use (or a concurrent loser) advances 0 rows and is rejected. A
    matched-but-replayed TOTP returns False WITHOUT falling through to backup
    codes (it is a TOTP, not a backup code).
    """
    from src.db.models import MfaBackupCode
    from src.utils.crypto import decrypt_field
    from src.utils.mfa import hash_backup_code, verify_totp_counter

    cleaned = (code or "").strip()

    # 1) TOTP — single-use via an atomic counter advance.
    if user.mfa_secret_encrypted:
        try:
            counter = verify_totp_counter(decrypt_field(user.mfa_secret_encrypted), cleaned)
        except Exception:
            # A corrupt/rotated secret must not 500 the login; fall through to
            # backup codes so a user is never hard-locked by a decrypt failure.
            counter = None
        if counter is not None:
            # Advance only if strictly newer than what we've already consumed.
            # WHERE uses the DB column (not user.mfa_last_totp_counter from a
            # stale prior SELECT) so concurrent same-code logins serialize on the
            # row and exactly one wins (Codex H2-P4). The mfa_enabled +
            # mfa_secret_encrypted guards make this UPDATE the single atomic gate:
            # if /mfa/disable committed between our SELECT and this UPDATE, the
            # row no longer matches and a stale challenge cannot mint a session
            # against a just-disabled account (Codex H2-P4 round 2).
            advanced = await db.execute(
                update(User)
                .where(
                    User.id == user.id,
                    User.mfa_enabled.is_(True),
                    User.mfa_secret_encrypted.is_not(None),
                    or_(
                        User.mfa_last_totp_counter.is_(None),
                        User.mfa_last_totp_counter < counter,
                    ),
                )
                .values(mfa_last_totp_counter=counter)
                .returning(User.id)
            )
            # A matched TOTP that did not advance is a replay (or the account was
            # disabled mid-flight) → reject; do NOT try it as a backup code.
            return advanced.scalar_one_or_none() is not None

    # 2) Backup code — atomic single-use consume. Look up by the keyed HMAC
    # digest (deterministic, same normalization as enrollment) and burn the row
    # in one statement; `RETURNING id` is non-empty iff a still-unused code
    # matched. Equality on the HMAC digest leaks nothing about the raw code.
    code_hash = hash_backup_code(cleaned)
    result = await db.execute(
        update(MfaBackupCode)
        .where(
            MfaBackupCode.user_id == user.id,
            MfaBackupCode.code_hash == code_hash,
            MfaBackupCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
        .returning(MfaBackupCode.id)
    )
    return result.scalar_one_or_none() is not None
