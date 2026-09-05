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
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_refresh_token, create_secure_token, hash_password
from src.api.middleware import audit_log, once_per, rate_limit, release_once
from src.api.schemas import RegisterResponse, TokenResponse, UserRegister, VerifyEmailRequest
from src.config import settings
from src.db import PendingRegistration, User
from src.utils.crypto import blind_index
from src.utils.logger import email_fingerprint, setup_logger

from .tokens import _VERIFY_TOKEN_EXPIRE_SECONDS, _decode_verify_token

_logger = setup_logger("api.auth.register")

# At most one duplicate-signup notice per address per 24h (email-bomb guard).
_DUP_SIGNUP_NOTICE_TTL = 86400

# Referral-code alphabet: excludes ambiguous chars (0/O, 1/I/L) so a shared code
# is unambiguous when read aloud.
_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Verified-flow registration never hashes a real password (the password is set at
# verify), so both the new-email and existing-email branches burn one bcrypt over
# this fixed dummy. The ~250ms constant dwarfs the one-DB-insert delta between the
# two branches, shrinking the residual timing oracle (Codex P2).
_TIMING_DUMMY_PASSWORD = "timing-parity-burn-not-a-real-password"


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
    # Anchor BOTH billing periods to the start of the current UTC month at
    # creation. Leaving these NULL is what let the daily quota rollover's
    # "IS NULL" arm zero a brand-new user's records_used inside their own
    # signup month (prod: 999 records billed on day 1, counter read 2 the next
    # morning). The columns also carry a server_default + NOT NULL as of
    # migration 086; this is the explicit belt to that suspenders, so the value
    # is right even when the ORM sends the column.
    _now = datetime.now(UTC)
    _period_start = _now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        first_name=first_name,
        last_name=last_name,
        password_hash=password_hash,
        plan="pro",
        records_limit=settings.PLAN_LIMITS["pro"],  # Pro limit during the 7-day trial
        trial_ends_at=_now + timedelta(days=7),
        records_period_start=_period_start,
        skip_trace_period_start=_period_start,
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
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> TokenResponse | RegisterResponse:
    """Dispatch to the legacy or the enumeration-safe registration flow."""
    await rate_limit(request, zone="auth")
    if settings.EMAIL_VERIFICATION_ENABLED:
        return await _register_user_verified(body, request, response, background_tasks, db)
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
    # Password AND name are REQUIRED in the legacy flow (all three are Optional on
    # the schema only to support the verified flow, which collects them at verify
    # time). Reject a missing one with 422 BEFORE any duplicate lookup / bcrypt /
    # insert, mirroring the password guard.
    if body.password is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password is required.",
        )
    if body.first_name is None or body.last_name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="First and last name are required.",
        )

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
    response: Response,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> RegisterResponse:
    """Enumeration-safe flow (EMAIL_VERIFICATION_ENABLED on): NEVER reveal whether
    the email exists. Both paths return the SAME neutral 200 with NO tokens.

    Existing email -> 'you already have an account' email. New email -> stage a
    fresh pending_registrations row and email a verification link. The bcrypt
    burn is on BOTH paths so the status/body/message oracle is closed.

    TIMING NOTE (accepted residual): both paths burn bcrypt (the ~250ms dominant
    cost) and touch ONLY Postgres inline — the existing-account notice runs as a
    post-response BACKGROUND task (not awaited), so neither path blocks on Redis.
    That matters because Redis can be down while Postgres is up: awaiting the
    notice's once_per inline (the prior behavior) made the existing-email path
    HANG on a Redis outage while the new-email path returned fast — a real,
    outage-amplified timing oracle (Codex). The only remaining asymmetry is the
    new path's extra ~2ms Postgres insert, a stable sub-bcrypt constant; fully
    flattening it needs measured constant-time padding (tracked as a follow-up).
    """
    # The neutral 200 status (vs the legacy 201): set it on BOTH the existing and
    # the new path so they are identical.
    response.status_code = status.HTTP_200_OK

    existing = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email))
    )
    if existing.scalar_one_or_none():
        hash_password(_TIMING_DUMMY_PASSWORD)  # timing parity with the new path
        _logger.info(
            "register (verify flow) rejected: account already exists (fp=%s)",
            email_fingerprint(body.email),
        )
        # Off the request path: scheduled AFTER the response is sent so its Redis
        # once_per + Celery enqueue can't add inline latency (and a Redis outage
        # can't stall the existing-email response). This branch returns (does not
        # raise), so the background task actually runs.
        background_tasks.add_task(_notify_existing_account, body.email)
        return RegisterResponse()  # neutral 200 — identical to the new-email path

    # New email: stage the signup as its OWN pending row (NOT a real users row,
    # and NOT an upsert). NO password, NO name, and NO referral are stored — ALL
    # of those are set at verify by whoever proves email control. Carrying the
    # password closed account pre-hijacking; carrying the NAME is what let an
    # attacker-initiated signup set a victim-verified account's display name, so
    # the name now also comes from the verifier (the columns are nullable as of
    # migration 076 and left NULL here). An attacker-supplied referral must never
    # benefit from a victim-verified account (the financial vector Codex flagged),
    # so it is dropped too. Independent rows per attempt mean an attacker
    # submitting this address lands in a SEPARATE row whose link is emailed to the
    # address owner; first verification wins via UNIQUE(users.email_hmac) and
    # siblings drop at verify.
    hash_password(_TIMING_DUMMY_PASSWORD)  # timing parity with the existing path
    pending = PendingRegistration(
        id=str(uuid.uuid4()),
        email=body.email,  # @validates fills email_hmac
        expires_at=datetime.now(UTC) + timedelta(seconds=_VERIFY_TOKEN_EXPIRE_SECONDS),
        # email_dispatch_state='pending' + next_email_attempt_at=now() come from
        # the column server-defaults: the row IS the outbox. The verification
        # email is sent by the `dispatch_pending_verification_emails` beat (mints
        # the token + sends + records the outcome on this row), NOT enqueued from
        # the request path. That deliberately keeps Redis (the Celery broker)
        # OFF the signup path entirely: a `.delay()` here would be LOST if Redis
        # were down at signup time AND its connection-retry could perturb the
        # neutral-200 timing. With the DB row as the durable record, a signup
        # made during a Redis outage is simply drained and sent on the next beat
        # tick after Redis recovers. The per-address email-bomb guard also moves
        # to the dispatcher (a Postgres advisory lock over real sends), so it no
        # longer depends on Redis being up either.
    )
    db.add(pending)
    await db.commit()

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
    if not pending_id:
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
        # NOTE: no separate Redis single-use gate is needed. The token's `sub` is
        # THIS pending row; once a verify creates the account it deletes every
        # pending row for the address, so any later click of the same (or a
        # sibling) link finds no row and lands here. UNIQUE(users.email_hmac) is
        # the authority that prevents a second account under any race.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    # Capture the (decrypted) pending email/hmac BEFORE the create attempt — a
    # later rollback would expire the ORM object and a re-access could re-query/
    # fail. NOTE: neither the password NOR the name is read from the pending row;
    # both come from the request (set by whoever proves email control), which is
    # what closes pre-hijacking AND attacker-chosen display names.
    email = pending.email
    email_hmac = pending.email_hmac
    first_name = body.first_name
    last_name = body.last_name

    # No referral attribution in the verification flow: the pending row carries no
    # ref, so an attacker-supplied code can never credit a victim-verified account.
    referred_by_id = None
    referral_code = await _generate_referral_code(db)
    try:
        user = await _create_real_user(
            db,
            email=email,
            first_name=first_name,
            last_name=last_name,
            password_hash=hash_password(body.new_password),
            referred_by_id=referred_by_id,
            referral_code=referral_code,
        )
    except IntegrityError as exc:
        await db.rollback()
        fields = _integrity_error_fields(exc)
        if not _is_duplicate_email_violation(fields):
            # NOT the users.email_hmac race (e.g. a referral_code unique
            # collision). Do NOT claim the email is verified or drop the pending
            # rows — surface it (the generic 500 handler returns a reference id).
            _logger.warning(
                "verify user-insert IntegrityError (non-duplicate): %s",
                fields or "no structured driver fields",
            )
            raise
        # A real account for this email already exists (a concurrent verify of a
        # sibling link won, or the user registered another way). Drop ALL pending
        # rows for the address and send them to login — never two accounts.
        await db.execute(
            delete(PendingRegistration).where(PendingRegistration.email_hmac == email_hmac)
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This email is already verified. Please log in.",
        ) from None

    # Drop every pending row for this address (this attempt + any siblings) in the
    # SAME transaction as the user insert, so a crash can't leave a real user AND
    # a redeemable pending row, and a sibling link can't mint a second session.
    await db.execute(
        delete(PendingRegistration).where(PendingRegistration.email_hmac == email_hmac)
    )
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
