"""Body logic for POST /auth/register. Extracted VERBATIM from auth.py — the
route decorator + signature stay in auth.py; this holds the moved handler body.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_refresh_token, create_secure_token, hash_password
from src.api.middleware import audit_log, rate_limit
from src.api.schemas import TokenResponse, UserRegister
from src.config import settings
from src.db import User
from src.utils.crypto import blind_index


async def register_user(
    body: UserRegister,
    request: Request,
    db: AsyncSession,
) -> TokenResponse:
    await rate_limit(request, zone="auth")

    # Check for duplicate — but return generic error (no user enumeration).
    # H3: look up by the email blind index (email is encrypted; the plaintext
    # value is no longer directly matchable). The DB UNIQUE(email_hmac) is the
    # race-safe authority — see the IntegrityError catch on flush below.
    existing = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email))
    )
    if existing.scalar_one_or_none():
        # A4: constant-time parity with the success path. The new-account
        # branch below runs bcrypt via hash_password(); burn an equivalent
        # bcrypt cost here so response latency cannot distinguish a
        # registered email (otherwise fast — returns before any hashing)
        # from a new one (slow), closing the account-enumeration timing
        # oracle that the generic error message alone does not.
        hash_password(body.password)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        )

    trial_end = datetime.now(UTC) + timedelta(days=7)

    # Sprint 7.3: resolve the referral code (if any) to a referrer user.
    # Unknown codes are silently dropped — we don't leak whether a
    # code exists, and a bad code must not block signup.
    referred_by_id: str | None = None
    if body.ref:
        referrer_res = await db.execute(
            select(User).where(
                User.referral_code == body.ref,
                User.is_active,
            )
        )
        referrer = referrer_res.scalar_one_or_none()
        if referrer is not None:
            referred_by_id = referrer.id

    # Generate a unique 8-char referral code for the new user. The
    # alphabet excludes ambiguous characters (0/O, 1/I/L) so the code
    # is unambiguous when shared verbally. Retry on collision — the
    # DB unique constraint is the final authority but we pre-check to
    # avoid most round-trips.
    import secrets
    _ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

    async def _generate_referral_code() -> str:
        for _ in range(8):
            candidate = "".join(secrets.choice(_ALPHABET) for _ in range(8))
            existing_code = await db.execute(
                select(User).where(User.referral_code == candidate)
            )
            if existing_code.scalar_one_or_none() is None:
                return candidate
        # Extremely unlikely after 8 tries with a 30^8 keyspace
        raise RuntimeError("Failed to generate unique referral code")

    referral_code = await _generate_referral_code()

    user = User(
        id=str(uuid.uuid4()),
        email=body.email,
        password_hash=hash_password(body.password),
        plan="pro",
        records_limit=settings.PLAN_LIMITS["pro"],  # Pro limit during the 7-day trial
        trial_ends_at=trial_end,
        referral_code=referral_code,
        referred_by_user_id=referred_by_id,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # H3: a concurrent signup with the same email won the UNIQUE(email_hmac)
        # race. The pre-check above is not race-safe on its own — the DB
        # constraint is the authority. Roll back and return the SAME generic
        # error as the duplicate branch (no user enumeration).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        ) from None

    # Fresh password-only session (H2-P5): amr=["pwd"], auth_time=now (default).
    token = create_secure_token(user.id, amr=["pwd"])
    refresh = create_refresh_token(user.id, amr=["pwd"])
    audit_log(request, "register", user.id)

    # Send welcome email (non-blocking — failure must not break registration)
    try:
        from src.workers.onboarding_emails import send_welcome_email
        send_welcome_email(body.email)
    except Exception:
        pass

    return TokenResponse(access_token=token, refresh_token=refresh)
