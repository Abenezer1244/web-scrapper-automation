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
from src.api.middleware import audit_log, once_per, rate_limit, release_once
from src.api.schemas import TokenResponse, UserRegister
from src.config import settings
from src.db import User
from src.utils.crypto import blind_index
from src.utils.logger import email_fingerprint, setup_logger

_logger = setup_logger("api.auth.register")

# At most one duplicate-signup notice per address per 24h (email-bomb guard).
_DUP_SIGNUP_NOTICE_TTL = 86400


def _integrity_error_fields(exc: IntegrityError) -> dict[str, str]:
    """Pull ONLY schema-controlled, PII-free diagnostics off an asyncpg
    IntegrityError so a failed insert is debuggable from logs without ever
    emitting row values. asyncpg exposes the structured fields on the driver
    error (``exc.orig``) or its ``__cause__``; we never log ``str(exc)`` /
    ``detail`` because those can echo the offending values.
    """
    fields: dict[str, str] = {}
    orig = getattr(exc, "orig", None)
    for obj in (orig, getattr(orig, "__cause__", None)):
        if obj is None:
            continue
        for name in ("sqlstate", "constraint_name", "table_name", "column_name"):
            val = getattr(obj, name, None)
            if val and name not in fields:
                fields[name] = str(val)
    return fields


def _is_duplicate_email_violation(fields: dict[str, str]) -> bool:
    """True iff an IntegrityError is specifically the email_hmac UNIQUE race —
    i.e. a concurrent signup for an address that ALREADY exists. We must NOT
    treat other violations (referral_code race, a not-null/check/enum failure)
    as duplicates: telling that user 'you already have an account' would be
    false. Matches on SQLSTATE 23505 (unique_violation) + the email_hmac
    constraint name (substring — robust to the exact generated name).
    """
    if fields.get("sqlstate") != "23505":
        return False
    # Verified constraint name: migration 053 creates UNIQUE "users_email_hmac_key".
    # Substring match stays correct across the default-named index too. If the
    # name ever changes, the race branch degrades safely (still 400, just no
    # notice) rather than mis-firing on the wrong constraint.
    return "email_hmac" in (fields.get("constraint_name") or "")


async def _notify_existing_account(email: str) -> None:
    """Best-effort out-of-band notice to the inbox owner of an EXISTING account
    that a signup was attempted for their address (the client only ever sees the
    generic 400 — no enumeration). Enqueued as a Celery task so the actual send
    is off the request path. Gated to at most once per address per 24h
    (email-bomb guard, IP-independent). Never raises — registration must still
    return its 400 even if Redis/broker are down.
    """
    fp = email_fingerprint(email)
    key = f"dupsignup:{fp}"
    # Claim the 24h gate atomically (SET NX). once_per fails CLOSED on a Redis
    # error, so a Redis outage simply skips the notice rather than spamming.
    if not await once_per(key, _DUP_SIGNUP_NOTICE_TTL):
        return
    try:
        from src.workers.onboarding_emails import send_duplicate_signup_email
        send_duplicate_signup_email.delay(email)
    except Exception as exc:  # noqa: BLE001 — never break the 400 on a broker blip
        # The gate was claimed but the enqueue failed: RELEASE it so the next
        # attempt within 24h can retry, and log a PII-safe warning (class only).
        _logger.warning(
            "dup-signup notify enqueue failed (fp=%s): %s", fp, type(exc).__name__
        )
        await release_once(key)


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
        # Observability: this is the #1 cause of a user-reported "Registration
        # failed" — they already have an account. Log a PII-safe fingerprint
        # (never the plaintext email) so ops can confirm it from logs alone.
        _logger.info(
            "register rejected: account already exists (fp=%s)",
            email_fingerprint(body.email),
        )
        # Out-of-band notice to the inbox owner so a legitimate returning user
        # learns to log in / reset instead of being stuck on the generic 400.
        # The send is enqueued (off the request path); the gate adds only a single
        # ~1ms Redis SET — negligible against the bcrypt burn above, and the
        # new-account path already does strictly more work (DB writes + an inline
        # welcome email), so this adds no existing-vs-new timing signal beyond
        # what that pre-existing asymmetry already implies.
        await _notify_existing_account(body.email)
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
        # Already sanitized + None-on-blank by UserRegister.name_validation.
        name=body.name,
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
    except IntegrityError as exc:
        # H3: a concurrent signup with the same email won the UNIQUE(email_hmac)
        # race. The pre-check above is not race-safe on its own — the DB
        # constraint is the authority. Roll back and return the SAME generic
        # error as the duplicate branch (no user enumeration).
        await db.rollback()
        fields = _integrity_error_fields(exc)
        # Observability: log the PII-free constraint/SQLSTATE so a genuine insert
        # failure (not-null, check, enum, or a referral-code race) is diagnosable
        # from logs — the generic 400 alone left this branch a black box.
        _logger.warning(
            "register insert failed (IntegrityError): %s",
            fields or "no structured driver fields",
        )
        # If (and ONLY if) this was the email_hmac UNIQUE race — a concurrent
        # signup for an already-existing address — give the inbox owner the same
        # duplicate-account notice as the pre-check branch. Other violations
        # (referral_code race, not-null/check) are NOT duplicates and must not
        # trigger a "you already have an account" email.
        if _is_duplicate_email_violation(fields):
            await _notify_existing_account(body.email)
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
