"""Body logic for POST /auth/register and POST /auth/verify-email.

Two registration modes, selected by settings.EMAIL_VERIFICATION_ENABLED:

  * OFF (legacy, default): register creates the account immediately and returns
    session tokens (201). A duplicate email returns a generic 400 (the message +
    timing are enumeration-safe, but the 201-vs-400 status is itself an oracle).

  * ON (enumeration-safe): register returns the SAME neutral 200 for a new vs
    existing email and issues NO tokens. A new signup is staged in
    pending_registrations (NOT a real users row — closes account-squatting); the
    account is created only when the emailed verification link is redeemed at
    /auth/verify-email, which then auto-logs-in.

The route decorators + signatures stay in auth.py; this holds the moved bodies.
"""

import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_refresh_token, create_secure_token, hash_password
from src.api.middleware import audit_log, once_per, rate_limit, release_once
from src.api.schemas import RegisterResponse, TokenResponse, UserRegister, VerifyEmailRequest
from src.config import settings
from src.db import PendingRegistration, User
from src.utils.crypto import blind_index
from src.utils.logger import email_fingerprint, setup_logger

from .tokens import _VERIFY_TOKEN_EXPIRE_SECONDS, _decode_verify_token, _mint_verify_token

_logger = setup_logger("api.auth.register")

# At most one duplicate-signup notice per address per 24h (email-bomb guard).
_DUP_SIGNUP_NOTICE_TTL = 86400

# Referral-code alphabet: excludes ambiguous chars (0/O, 1/I/L) so a shared code
# is unambiguous when read aloud.
_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


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
    """True iff an IntegrityError is specifically the users.email_hmac UNIQUE race
    — i.e. a concurrent signup for an address that ALREADY exists. We must NOT
    treat other violations (referral_code race, a not-null/check/enum failure)
    as duplicates: telling that user 'you already have an account' would be
    false. Matches on SQLSTATE 23505 (unique_violation) + the email_hmac
    constraint name (substring — robust to the exact generated name).
    """
    if fields.get("sqlstate") != "23505":
        return False
    # Verified constraint name: migration 053 creates UNIQUE "users_email_hmac_key".
    # Substring match stays correct across the default-named index too. If the
    # name ever changes, the race branch degrades safely (still generic) rather
    # than mis-firing on the wrong constraint.
    return "email_hmac" in (fields.get("constraint_name") or "")


async def _notify_existing_account(email: str) -> None:
    """Best-effort out-of-band notice to the inbox owner of an EXISTING account
    that a signup was attempted for their address (the client only ever sees the
    generic response — no enumeration). Enqueued as a Celery task so the actual
    send is off the request path. Gated to at most once per address per 24h
    (email-bomb guard, IP-independent). Never raises — registration must still
    return its response even if Redis/broker are down.
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
    except Exception as exc:  # noqa: BLE001 — never break the response on a broker blip
        # The gate was claimed but the enqueue failed: RELEASE it so the next
        # attempt within 24h can retry, and log a PII-safe warning (class only).
        _logger.warning(
            "dup-signup notify enqueue failed (fp=%s): %s", fp, type(exc).__name__
        )
        await release_once(key)


async def _generate_referral_code(db: AsyncSession) -> str:
    """Generate a unique 8-char referral code. The DB unique constraint is the
    final authority; we pre-check to avoid most round-trips. Retry on collision."""
    for _ in range(8):
        candidate = "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))
        existing = await db.execute(select(User).where(User.referral_code == candidate))
        if existing.scalar_one_or_none() is None:
            return candidate
    # Extremely unlikely after 8 tries with a 30^8 keyspace.
    raise RuntimeError("Failed to generate unique referral code")


async def _resolve_referrer_id(db: AsyncSession, ref_code: str | None) -> str | None:
    """Resolve a referral code to an active referrer's id. Unknown codes are
    silently dropped — we never leak whether a code exists, and a bad code must
    not block signup."""
    if not ref_code:
        return None
    res = await db.execute(
        select(User).where(User.referral_code == ref_code, User.is_active)
    )
    referrer = res.scalar_one_or_none()
    return referrer.id if referrer is not None else None


async def _create_real_user(
    db: AsyncSession,
    *,
    email: str,
    first_name: str,
    last_name: str,
    password_hash: str,
    referred_by_id: str | None,
    referral_code: str,
) -> User:
    """Insert the real users row (Pro 7-day trial) and flush. Raises
    IntegrityError on the email_hmac UNIQUE race — the caller decides how to map
    it (generic error vs 'already registered, please log in')."""
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        first_name=first_name,
        last_name=last_name,
        password_hash=password_hash,
        plan="pro",
        records_limit=settings.PLAN_LIMITS["pro"],  # Pro limit during the 7-day trial
        trial_ends_at=datetime.now(UTC) + timedelta(days=7),
        referral_code=referral_code,
        referred_by_user_id=referred_by_id,
    )
    db.add(user)
    await db.flush()
    return user


async def register_user(
    body: UserRegister,
    request: Request,
    response: Response,
    db: AsyncSession,
) -> TokenResponse | RegisterResponse:
    """Dispatch to the legacy or the enumeration-safe registration flow."""
    await rate_limit(request, zone="auth")
    if settings.EMAIL_VERIFICATION_ENABLED:
        return await _register_user_verified(body, request, db)
    return await _register_user_legacy(body, request, response, db)


async def _register_user_legacy(
    body: UserRegister,
    request: Request,
    response: Response,
    db: AsyncSession,
) -> TokenResponse:
    """Legacy flow (EMAIL_VERIFICATION_ENABLED off): create the account
    immediately and return session tokens (201). Behavior is unchanged from
    before the verification feature."""
    # Check for duplicate — generic error (no user enumeration). The DB
    # UNIQUE(email_hmac) is the race-safe authority (IntegrityError catch below).
    existing = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email))
    )
    if existing.scalar_one_or_none():
        # A4: constant-time parity with the success path — burn an equivalent
        # bcrypt cost so latency can't distinguish a registered email from a new
        # one (the generic message alone does not close the timing oracle).
        hash_password(body.password)
        _logger.info(
            "register rejected: account already exists (fp=%s)",
            email_fingerprint(body.email),
        )
        await _notify_existing_account(body.email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        )

    referred_by_id = await _resolve_referrer_id(db, body.ref)
    referral_code = await _generate_referral_code(db)
    try:
        user = await _create_real_user(
            db,
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            password_hash=hash_password(body.password),
            referred_by_id=referred_by_id,
            referral_code=referral_code,
        )
    except IntegrityError as exc:
        # A concurrent signup won the UNIQUE(email_hmac) race. Roll back and
        # return the SAME generic error as the duplicate branch (no enumeration).
        await db.rollback()
        fields = _integrity_error_fields(exc)
        _logger.warning(
            "register insert failed (IntegrityError): %s",
            fields or "no structured driver fields",
        )
        if _is_duplicate_email_violation(fields):
            await _notify_existing_account(body.email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        ) from None

    await db.commit()
    # Fresh password-only session (H2-P5): amr=["pwd"], auth_time=now (default).
    token = create_secure_token(user.id, amr=["pwd"])
    refresh = create_refresh_token(user.id, amr=["pwd"])
    audit_log(request, "register", user.id)

    # Welcome email (non-blocking — failure must not break registration).
    try:
        from src.workers.onboarding_emails import send_welcome_email
        send_welcome_email(body.email)
    except Exception:
        pass

    response.status_code = status.HTTP_201_CREATED
    return TokenResponse(access_token=token, refresh_token=refresh)


async def _register_user_verified(
    body: UserRegister,
    request: Request,
    db: AsyncSession,
) -> RegisterResponse:
    """Enumeration-safe flow (EMAIL_VERIFICATION_ENABLED on): NEVER reveal whether
    the email exists. Both paths return the SAME neutral 200 with NO tokens.

    Existing email -> 'you already have an account' email. New email -> stage a
    pending_registrations row (upsert) and email a verification link. The bcrypt
    burn is on BOTH paths so response latency cannot distinguish them.
    """
    existing = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email))
    )
    if existing.scalar_one_or_none():
        hash_password(body.password)  # timing parity with the new-email path
        _logger.info(
            "register (verify flow) rejected: account already exists (fp=%s)",
            email_fingerprint(body.email),
        )
        await _notify_existing_account(body.email)
        return RegisterResponse()  # neutral 200 — identical to the new-email path

    # New email: stage the signup (NOT a real users row). UPSERT by email_hmac so
    # a repeat signup before verifying replaces the pending data (and re-sends a
    # link) without creating a second row; UNIQUE(email_hmac) makes it race-safe.
    password_hash = hash_password(body.password)
    expires_at = datetime.now(UTC) + timedelta(seconds=_VERIFY_TOKEN_EXPIRE_SECONDS)
    table = PendingRegistration.__table__
    ins = pg_insert(table).values(
        id=str(uuid.uuid4()),
        email=body.email,
        email_hmac=blind_index(body.email),
        first_name=body.first_name,
        last_name=body.last_name,
        password_hash=password_hash,
        ref_code=body.ref,
        expires_at=expires_at,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[table.c.email_hmac],
        set_={
            # Keep the existing row id (so an already-emailed earlier link for
            # this address still resolves); refresh everything else + the expiry.
            "email": ins.excluded.email,
            "first_name": ins.excluded.first_name,
            "last_name": ins.excluded.last_name,
            "password_hash": ins.excluded.password_hash,
            "ref_code": ins.excluded.ref_code,
            "expires_at": ins.excluded.expires_at,
        },
    ).returning(table.c.id)
    result = await db.execute(stmt)
    pending_id = result.scalar_one()
    await db.commit()

    token = _mint_verify_token(pending_id)
    verify_link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    # Enqueue off the request path (Celery) so the Resend latency can't become an
    # existing-vs-new timing oracle and a broker blip can't break the response.
    try:
        from src.workers.onboarding_emails import send_verification_email
        send_verification_email.delay(body.email, verify_link)
    except Exception as exc:  # noqa: BLE001 — never break the neutral 200
        _logger.warning(
            "verification email enqueue failed (fp=%s): %s",
            email_fingerprint(body.email), type(exc).__name__,
        )
    audit_log(request, "register_pending", None)
    return RegisterResponse()


async def verify_user_email(
    body: VerifyEmailRequest,
    request: Request,
    db: AsyncSession,
) -> TokenResponse:
    """Redeem an email-verification link: validate the single-use token, create
    the real account from the pending row, and auto-login (mint session tokens).
    """
    await rate_limit(request, zone="auth")
    import jwt

    # Catch ONLY the JWT error -> generic 400. Any other exception (e.g. a
    # SECRET_KEY misconfiguration) must surface as 500, not be masked.
    try:
        payload = _decode_verify_token(body.token)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    pending_id: str = payload.get("sub", "")
    jti: str = payload.get("jti", "")
    exp: int = payload.get("exp", 0)
    ttl = max(0, exp - int(time.time()))
    if not pending_id or not jti or ttl <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    now = datetime.now(UTC)
    res = await db.execute(
        select(PendingRegistration).where(
            PendingRegistration.id == pending_id,
            PendingRegistration.expires_at > now,
        )
    )
    pending = res.scalar_one_or_none()
    if pending is None:
        # Already redeemed (row deleted), expired, or never existed — all generic.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    # Single-use gate (atomic, fail-closed). Burning the jti FIRST means exactly
    # one of N concurrent redemptions of the SAME token proceeds; the rest get
    # "already used". consume_once raises on Redis error -> 503 (never create an
    # account we can't prove the link was un-redeemed for).
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    try:
        if not await TokenBlacklist.consume_once(jti, ttl):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This verification link has already been used. Please log in.",
            )
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # Read the (decrypted) pending fields before any write that could detach it.
    email = pending.email
    referred_by_id = await _resolve_referrer_id(db, pending.ref_code)
    referral_code = await _generate_referral_code(db)
    try:
        user = await _create_real_user(
            db,
            email=email,
            first_name=pending.first_name,
            last_name=pending.last_name,
            password_hash=pending.password_hash,
            referred_by_id=referred_by_id,
            referral_code=referral_code,
        )
    except IntegrityError:
        # A real account for this email already exists (a different verification
        # link for the same address won, or the user registered another way).
        # Clean up the pending row and send them to login — never two accounts.
        await db.rollback()
        await db.execute(delete(PendingRegistration).where(PendingRegistration.id == pending_id))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This email is already verified. Please log in.",
        ) from None

    # Consume the pending row in the SAME transaction as the user insert so a
    # crash can't leave both a real user and a redeemable pending row.
    await db.execute(delete(PendingRegistration).where(PendingRegistration.id == pending_id))
    await db.commit()

    token = create_secure_token(user.id, amr=["pwd"])
    refresh = create_refresh_token(user.id, amr=["pwd"])
    audit_log(request, "register_verified", user.id)

    # Welcome email (non-blocking — failure must not break verification).
    try:
        from src.workers.onboarding_emails import send_welcome_email
        send_welcome_email(email)
    except Exception:
        pass

    return TokenResponse(access_token=token, refresh_token=refresh)
